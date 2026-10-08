"""Per-task disposable worktrees with branches as the durable deliverable.

Model (aligned with Multica's local worktree mode):

- Every task gets a fresh worktree of the user's repo, created inside a
  task-scoped env root. Nothing is reused across turns except the branch.
- The branch lives in the user's own repository and is the task's delivery.
  Finalize commits whatever the agent left uncommitted before the worktree
  is removed, so work is never silently discarded.
- The user's own directory is never written to. Their uncommitted state is
  captured as a snapshot commit (built in a private GIT_INDEX_FILE, so the
  capture never touches their index or waits on its lock) and replayed into
  the worktree.
- Continuing a branch is decided by the recorded checkpoint in the database
  (recorded_tip must still be an ancestor of the branch tip), never by the
  branch name alone. A hidden ref (refs/mica/local-state/<branch>) anchors
  the user snapshot so git gc cannot reclaim it between turns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120
MAX_UNTRACKED_FILES = 2000
MAX_UNTRACKED_BYTES = 200 * 1024 * 1024
LOCAL_STATE_REF_PREFIX = "refs/mica/local-state/"
ENV_META_NAME = ".mica-env.json"
WORKTREE_DIR_NAME = "worktree"

# Platform-made commits (snapshots, baselines, leftover commits) always carry
# this identity instead of the repo's: provenance stays visible in git log,
# and repos without user.email configured can still be committed into.
COMMIT_IDENTITY_ARGS = ["-c", "user.name=mica", "-c", "user.email=mica@localhost"]

# Agent CLIs may drop session state into their working directory (e.g. claude
# plugins write <cwd>/.omc). These dirs are runtime junk, not deliverables:
# finalize deletes them before committing, and the snapshot skips them.
AGENT_STATE_DIR_NAMES = (".omc",)

# `git add -A` races with agent processes that create-and-delete transient
# files (lock files): a file seen during the scan can vanish before it is
# stat'ed, which fails the whole add. A few retries settle it.
ADD_RETRY_ATTEMPTS = 3
ADD_RETRY_DELAY_SECONDS = 0.2


class WorktreeError(RuntimeError):
    pass


class WorktreeFinalizeError(WorktreeError):
    """Finalize could not deliver; the worktree is preserved for inspection."""


@dataclass
class PreparedWorktree:
    branch: str
    workdir: str
    env_root: str
    repo: str
    base_commit: str
    user_state: str
    created_branch: bool = False
    continued: bool = False
    replay_conflicts: List[str] = field(default_factory=list)


@dataclass
class DeliveryResult:
    branch: str
    tip: str
    delivered: bool


@dataclass
class _BranchPlan:
    name: str
    base: str
    continues: bool = False
    reset: bool = False
    prior_state: Optional[str] = None


class WorktreeManager:
    """Prepares, finalizes and sweeps per-task git worktrees."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self._locks: Dict[str, Lock] = {}
        self._guard = Lock()

    # ------------------------------------------------------------------ prepare

    def prepare(
        self,
        source_path: str,
        issue_id: str,
        agent_id: str,
        task_id: str,
        previous_branch: Optional[str] = None,
        previous_recorded_tip: Optional[str] = None,
    ) -> PreparedWorktree:
        repo = self._repository_root(source_path)
        with self._guard:
            lock = self._locks.setdefault(str(repo), Lock())
        with lock:
            with self._repository_lock(repo):
                return self._prepare_locked(
                    repo, issue_id, agent_id, task_id, previous_branch, previous_recorded_tip
                )

    def _prepare_locked(
        self,
        repo: Path,
        issue_id: str,
        agent_id: str,
        task_id: str,
        previous_branch: Optional[str],
        previous_recorded_tip: Optional[str],
    ) -> PreparedWorktree:
        head = self._git_quiet(repo, "rev-parse", "--verify", "HEAD")
        if head is None:
            raise WorktreeError(
                f"repository {repo} has no commit to branch from; "
                "make an initial commit or switch the project to in_place mode"
            )

        env_root = self._env_root(repo, task_id)
        if env_root.exists():
            # Same task re-dispatched: its previous attempt's env is scrap.
            self._remove_env_root(repo, env_root)
        env_root.mkdir(parents=True, exist_ok=True)
        worktree_path = env_root / WORKTREE_DIR_NAME

        # Self-heal registrations orphaned by a crashed daemon.
        self._git(repo, "worktree", "prune", check=False)

        self._check_untracked_replayable(repo)
        user_state = self._capture_user_snapshot(repo, env_root, head)
        plan = self._plan_branch(
            repo, issue_id, agent_id, task_id, previous_branch, previous_recorded_tip, head
        )

        created_branch = False
        try:
            created_branch = self._add_worktree(repo, worktree_path, plan)
            # What the checkout already carries of the user's directory: the
            # previous turn's snapshot for a continued branch, HEAD otherwise.
            # Falling back to HEAD on a continued branch with a lost record
            # degrades to conflicts the agent resolves — never to reverting the
            # branch's own work, which diffing against the tip would do.
            carried = plan.prior_state if (plan.continues and plan.prior_state) else head
            conflicts = self._replay_user_state(repo, worktree_path, carried, user_state)

            if conflicts:
                base_commit = plan.base
            else:
                dirty = bool(self._git(worktree_path, "status", "--porcelain").strip())
                if dirty or not plan.continues:
                    # The baseline separates the user's replayed WIP from the
                    # agent's own work, and marks a fresh branch as ours.
                    base_commit = self._commit_all(
                        worktree_path,
                        f"mica: task {task_id} baseline (user snapshot)",
                        allow_empty=True,
                    ) or plan.base
                else:
                    base_commit = plan.base
                self._write_branch_record(repo, plan.name, user_state, base_commit)

            self._write_meta(env_root, repo, plan.name, task_id)
        except Exception:
            self._remove_worktree_dir(repo, worktree_path)
            if created_branch:
                self._git(repo, "branch", "-D", plan.name, check=False)
            self._remove_dir(env_root)
            raise

        return PreparedWorktree(
            branch=plan.name,
            workdir=str(worktree_path),
            env_root=str(env_root),
            repo=str(repo),
            base_commit=base_commit,
            user_state=user_state,
            created_branch=created_branch,
            continued=plan.continues,
            replay_conflicts=conflicts,
        )

    def _plan_branch(
        self,
        repo: Path,
        issue_id: str,
        agent_id: str,
        task_id: str,
        previous_branch: Optional[str],
        previous_recorded_tip: Optional[str],
        head: str,
    ) -> _BranchPlan:
        name = previous_branch or self._branch_name(issue_id, agent_id)
        if self._git_quiet(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}") is None:
            return _BranchPlan(name=name, base=head)

        # The branch exists. Continuing it requires a checkpoint that is still
        # an ancestor of its tip: a branch with no record at all (legacy or
        # the user's own), or one deleted and recreated under the same name,
        # is never adopted.
        tip_ref = f"refs/heads/{name}"
        checkpoint = previous_recorded_tip
        if checkpoint is not None and not self._is_ancestor(repo, checkpoint, tip_ref):
            checkpoint = None
        if checkpoint is None:
            # finalize writes the branch record before the delivery reaches
            # the database, so a branch whose run crashed in between (or whose
            # provider failed before any delivery) carries its checkpoint only
            # here. Adopting it keeps the branch name stable across retries;
            # a foreign branch has no record and still forks below.
            recorded = self._read_record_tip(repo, name)
            if recorded is not None and self._is_ancestor(repo, recorded, tip_ref):
                checkpoint = recorded
        if checkpoint is None:
            alt = f"{name}-{task_id[:8]}"
            if self._git_quiet(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{alt}"):
                alt = f"{alt}-{int(time.time())}"
            logger.info("worktree: branch %s is not this task line's; forking %s", name, alt)
            return _BranchPlan(name=alt, base=head)

        # The user merged the branch: its tip carries nothing HEAD does not, so
        # continuing from it would strand this task behind their own commits.
        if self._is_ancestor(repo, name, "HEAD"):
            return _BranchPlan(name=name, base=head, reset=True)

        tip = self._git(repo, "rev-parse", f"refs/heads/{name}").strip()
        prior_state = self._read_prior_state(repo, name)
        if prior_state is None:
            logger.warning(
                "worktree: no snapshot record for %s; replaying against HEAD may conflict",
                name,
            )
        return _BranchPlan(name=name, base=tip, continues=True, prior_state=prior_state)

    def _add_worktree(self, repo: Path, path: Path, plan: _BranchPlan) -> bool:
        if plan.continues:
            args = ["worktree", "add", str(path), plan.name]
        elif plan.reset:
            args = ["worktree", "add", "-B", plan.name, str(path), plan.base]
        else:
            args = ["worktree", "add", "-b", plan.name, str(path), plan.base]
        completed = subprocess.run(
            ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True,
            timeout=GIT_TIMEOUT,
        )
        if completed.returncode == 0:
            return not plan.continues
        output = (completed.stderr or completed.stdout).strip()
        raise WorktreeError(f"git worktree add failed for branch {plan.name}: {output}")

    # ------------------------------------------------------------------ finalize

    def finalize(self, prepared: PreparedWorktree) -> DeliveryResult:
        """Commit leftovers, record the branch, remove the worktree."""
        repo = Path(prepared.repo)
        worktree_path = Path(prepared.workdir)
        env_root = Path(prepared.env_root)
        with self._guard:
            lock = self._locks.setdefault(str(repo), Lock())
        with lock:
            with self._repository_lock(repo):
                unmerged = self._unmerged_paths(worktree_path)
                if unmerged:
                    raise WorktreeFinalizeError(
                        f"unresolved merge in {unmerged}; refusing to deliver branch "
                        f"{prepared.branch}; the worktree is preserved at {worktree_path}"
                    )
                if self._git(worktree_path, "status", "--porcelain").strip():
                    try:
                        self._commit_all(
                            worktree_path, f"mica: task leftover commit ({prepared.branch})"
                        )
                    except WorktreeError as exc:
                        raise WorktreeFinalizeError(
                            f"could not commit the agent's changes on {prepared.branch}: {exc}; "
                            f"the worktree is preserved at {worktree_path}"
                        ) from exc
                tip = self._git(worktree_path, "rev-parse", "HEAD").strip()

                produced = tip != prepared.base_commit
                if not produced and prepared.created_branch:
                    # Read-only turn on a fresh branch: drop it so the user's
                    # branch list only grows for tasks that produced work.
                    self._remove_worktree_dir(repo, worktree_path)
                    self._git(repo, "branch", "-D", prepared.branch, check=False)
                    self._delete_branch_record(repo, prepared.branch)
                    self._remove_dir(env_root)
                    return DeliveryResult(branch="", tip="", delivered=False)

                self._write_branch_record(repo, prepared.branch, prepared.user_state, tip)
                self._remove_worktree_dir(repo, worktree_path)
                self._remove_dir(env_root)
                return DeliveryResult(branch=prepared.branch, tip=tip, delivered=produced)

    def release(self, prepared: PreparedWorktree) -> None:
        """Preserve a failed/cancelled task's files while freeing its branch.

        Renaming the worktree directory makes the next `worktree prune` drop
        the registration, so a follow-up task can check out the branch again.
        The files stay on disk for inspection until sweep() reclaims them.
        """
        repo = Path(prepared.repo)
        worktree_path = Path(prepared.workdir)
        try:
            if worktree_path.exists():
                preserved = worktree_path.with_name(WORKTREE_DIR_NAME + "-preserved")
                if preserved.exists():
                    shutil.rmtree(preserved)
                os.rename(worktree_path, preserved)
            self._git(repo, "worktree", "prune", check=False)
        except Exception:
            logger.warning("worktree: release failed for %s", prepared.workdir, exc_info=True)

    # ------------------------------------------------------------------ sweep

    def sweep(self, is_task_active: Callable[[str], bool]) -> None:
        """Reclaim env roots whose task is gone or terminal."""
        if not self.root.exists():
            return
        for repo_dir in sorted(self.root.iterdir()):
            if not repo_dir.is_dir():
                continue
            for env_root in sorted(repo_dir.iterdir()):
                if not env_root.is_dir():
                    continue
                meta = self._read_meta(env_root)
                if meta is None:
                    shutil.rmtree(env_root, ignore_errors=True)
                    continue
                if is_task_active(meta.get("task_id", "")):
                    continue
                repo = Path(meta["repo"])
                worktree_path = env_root / WORKTREE_DIR_NAME
                if worktree_path.exists():
                    self._git(repo, "worktree", "remove", "--force", str(worktree_path), check=False)
                self._git(repo, "worktree", "prune", check=False)
                shutil.rmtree(env_root, ignore_errors=True)
                branch = meta.get("branch") or ""
                if branch and not self._branch_exists(repo, branch):
                    self._delete_branch_record(repo, branch)
            try:
                repo_dir.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------ snapshot

    def _capture_user_snapshot(self, repo: Path, env_root: Path, head: str) -> str:
        """One commit whose tree is the user's working directory right now."""
        index_path = env_root / ".mica-snapshot-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
        try:
            # Seeding from the user's own index lets git trust its stat cache
            # instead of re-hashing the whole repo. Copied as a plain file:
            # going through git would want the index lock, which is exactly
            # what the private index exists to avoid.
            src = self._git_quiet(repo, "rev-parse", "--git-path", "index")
            if src:
                src_path = Path(src) if Path(src).is_absolute() else repo / src
                try:
                    shutil.copyfile(src_path, index_path)
                except OSError:
                    pass
            self._git_env(repo, env, "add", "-A")
            for name in AGENT_STATE_DIR_NAMES:
                # Drop agent-tooling session state from the snapshot without
                # touching the user's working tree or real index.
                self._git_env(repo, env, "rm", "-r", "-q", "--cached", "--ignore-unmatch", name)
            tree = self._git_env(repo, env, "write-tree").strip()
            return self._git(
                repo,
                *COMMIT_IDENTITY_ARGS,
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                "mica: local directory snapshot",
            ).strip()
        finally:
            index_path.unlink(missing_ok=True)

    def _replay_user_state(
        self, repo: Path, worktree_path: Path, carried: str, user_state: str
    ) -> List[str]:
        if self._git_ok(worktree_path, "diff", "--quiet", carried, user_state):
            return []
        increment = self._git(
            repo,
            *COMMIT_IDENTITY_ARGS,
            "commit-tree",
            f"{user_state}^{{tree}}",
            "-p",
            carried,
            "-m",
            "mica: local edits to replay",
        ).strip()
        completed = subprocess.run(
            ["git", "-C", str(worktree_path), "cherry-pick", "--no-commit", increment],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if completed.returncode == 0:
            return []
        conflicts = self._unmerged_paths(worktree_path)
        if not conflicts:
            raise WorktreeError(
                f"could not replay the user's local edits into the task worktree: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        # Unresolved merges stay in the worktree on purpose: the agent is the
        # only party that can judge which version is right, and the prompt
        # tells it to resolve them before anything else.
        return conflicts

    # ------------------------------------------------------------------ records

    def _write_branch_record(self, repo: Path, branch: str, user_state: str, tip: str) -> None:
        record = self._git(
            repo,
            *COMMIT_IDENTITY_ARGS,
            "commit-tree",
            f"{user_state}^{{tree}}",
            "-p",
            user_state,
            "-p",
            tip,
            "-m",
            "mica: task branch record\n\nTree: user snapshot this branch carries. "
            "Second parent: branch tip at record time.",
        ).strip()
        self._git(repo, "update-ref", self._state_ref(branch), record)

    def _read_prior_state(self, repo: Path, branch: str) -> Optional[str]:
        record = self._git_quiet(repo, "rev-parse", "--verify", "--quiet", self._state_ref(branch))
        if record is None:
            return None
        return self._git_quiet(repo, "rev-parse", "--verify", "--quiet", f"{record}^1")

    def _read_record_tip(self, repo: Path, branch: str) -> Optional[str]:
        """The branch tip checkpoint stored in the record's second parent."""
        record = self._git_quiet(repo, "rev-parse", "--verify", "--quiet", self._state_ref(branch))
        if record is None:
            return None
        return self._git_quiet(repo, "rev-parse", "--verify", "--quiet", f"{record}^2")

    def _delete_branch_record(self, repo: Path, branch: str) -> None:
        self._git(repo, "update-ref", "-d", self._state_ref(branch), check=False)

    # ------------------------------------------------------------------ helpers

    def _check_untracked_replayable(self, repo: Path) -> None:
        listing = self._git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        files = [name for name in listing.split("\0") if name]
        if len(files) > MAX_UNTRACKED_FILES:
            raise WorktreeError(
                f"{len(files)} untracked files in {repo} exceed the snapshot limit "
                f"({MAX_UNTRACKED_FILES}); ignore or remove some before running a task"
            )
        total = 0
        for name in files:
            try:
                total += (repo / name).stat().st_size
            except OSError:
                continue
            if total > MAX_UNTRACKED_BYTES:
                raise WorktreeError(
                    f"untracked content in {repo} exceeds the snapshot limit "
                    f"({MAX_UNTRACKED_BYTES // (1024 * 1024)} MiB); ignore or remove some first"
                )

    def _unmerged_paths(self, worktree_path: Path) -> List[str]:
        out = self._git(worktree_path, "diff", "--name-only", "--diff-filter=U", check=False)
        return [line for line in out.splitlines() if line.strip()]

    def _commit_all(self, worktree_path: Path, message: str, allow_empty: bool = False) -> Optional[str]:
        for name in AGENT_STATE_DIR_NAMES:
            shutil.rmtree(worktree_path / name, ignore_errors=True)
        last_error: Optional[WorktreeError] = None
        for _ in range(ADD_RETRY_ATTEMPTS):
            try:
                self._git(worktree_path, "add", "-A")
                last_error = None
                break
            except WorktreeError as exc:
                last_error = exc
                time.sleep(ADD_RETRY_DELAY_SECONDS)
        if last_error is not None:
            raise last_error
        if not allow_empty and not self._git(worktree_path, "status", "--porcelain").strip():
            return None
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.insert(1, "--allow-empty")
        self._git(worktree_path, *COMMIT_IDENTITY_ARGS, *args)
        return self._git(worktree_path, "rev-parse", "HEAD").strip()

    def _is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
            check=False,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        return completed.returncode == 0

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        return (
            self._git_quiet(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
            is not None
        )

    def _remove_worktree_dir(self, repo: Path, worktree_path: Path) -> None:
        self._git(repo, "worktree", "remove", "--force", str(worktree_path), check=False)
        shutil.rmtree(worktree_path, ignore_errors=True)
        self._git(repo, "worktree", "prune", check=False)

    def _remove_env_root(self, repo: Path, env_root: Path) -> None:
        worktree_path = env_root / WORKTREE_DIR_NAME
        if worktree_path.exists():
            self._remove_worktree_dir(repo, worktree_path)
        self._remove_dir(env_root)

    @staticmethod
    def _remove_dir(env_root: Path) -> None:
        shutil.rmtree(env_root, ignore_errors=True)
        try:
            # Drop the per-repo parent directory once its last env root is gone.
            env_root.parent.rmdir()
        except OSError:
            pass

    def _write_meta(self, env_root: Path, repo: Path, branch: str, task_id: str) -> None:
        meta = {"repo": str(repo), "branch": branch, "task_id": task_id}
        (env_root / ENV_META_NAME).write_text(json.dumps(meta), encoding="utf-8")

    @staticmethod
    def _read_meta(env_root: Path) -> Optional[Dict[str, str]]:
        try:
            return json.loads((env_root / ENV_META_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _env_root(self, repo: Path, task_id: str) -> Path:
        repo_key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
        return self.root / repo_key / task_id

    @staticmethod
    def _state_ref(branch: str) -> str:
        return LOCAL_STATE_REF_PREFIX + branch

    def _repository_root(self, source_path: str) -> Path:
        source = Path(source_path).expanduser().resolve()
        if not source.is_dir():
            raise WorktreeError(f"project path does not exist: {source}")
        root = self._git(source, "rev-parse", "--show-toplevel").strip()
        return Path(root).resolve()

    @classmethod
    def _branch_name(cls, issue_id: str, agent_id: str) -> str:
        return f"mica/{cls._slug(issue_id)}/{cls._slug(agent_id)}"

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
        return (slug or "task")[:48]

    @contextmanager
    def _repository_lock(self, source: Path):
        common_dir_value = self._git(source, "rev-parse", "--git-common-dir").strip()
        common_dir = Path(common_dir_value)
        if not common_dir.is_absolute():
            common_dir = (source / common_dir).resolve()
        lock_path = common_dir / "mica-worktree.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                import fcntl
            except ModuleNotFoundError:
                yield
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _git_ok(cwd: Path, *args: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        return completed.returncode == 0

    @staticmethod
    def _git(cwd: Path, *args: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if check and completed.returncode != 0:
            raise WorktreeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
        return completed.stdout

    @staticmethod
    def _git_env(cwd: Path, env: Dict[str, str], *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env=env,
        )
        if completed.returncode != 0:
            raise WorktreeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
        return completed.stdout

    @staticmethod
    def _git_quiet(cwd: Path, *args: str) -> Optional[str]:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None
