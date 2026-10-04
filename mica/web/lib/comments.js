// Preserve server order while grouping every reply under its root comment.
export function commentThreads(comments) {
  const byId = new Map(comments.map((comment) => [comment.id, comment]));
  const order = new Map(comments.map((comment, index) => [comment.id, index]));
  const groups = new Map();
  for (const comment of comments) {
    let root = comment;
    const visited = [];
    while (root.parent_comment_id && byId.has(root.parent_comment_id)) {
      if (visited.includes(root.id)) {
        root = byId.get(
          visited
            .slice(visited.indexOf(root.id))
            .sort((a, b) => order.get(a) - order.get(b))[0],
        );
        break;
      }
      visited.push(root.id);
      root = byId.get(root.parent_comment_id);
    }
    if (!groups.has(root.id)) groups.set(root.id, { root, replies: [] });
    if (comment.id !== root.id) groups.get(root.id).replies.push(comment);
  }
  return [...groups.values()];
}
