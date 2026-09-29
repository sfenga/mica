const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const api = async (url, options = {}) => {
  const response = await fetch(url, options);
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(data?.detail || text || `请求失败 (${response.status})`);
  return data;
};
const jsonOptions = (method, body) => ({method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});

const ISSUE_COLUMNS = [
  ["backlog","待规划"], ["todo","待执行"], ["in_progress","执行中"],
  ["in_review","待确认"], ["blocked","已阻塞"], ["done","已完成"],
];
const ISSUE_LABEL = Object.fromEntries([...ISSUE_COLUMNS, ["cancelled","已取消"]]);
const TASK_LABEL = {queued:"排队中",dispatched:"已派发",running:"执行中",waiting_input:"等待输入",completed:"已完成",failed:"失败",cancelled:"已取消"};
const ACTIVE_TASKS = new Set(["queued","dispatched","running","waiting_input"]);
const USER_MOVES = {backlog:["todo"],todo:["backlog"],in_review:["done","in_progress","blocked"],blocked:["todo","in_progress"]};
const routeMeta = {
  board:["MISSION CONTROL","任务看板","＋ 新建任务","issue"],
  agents:["AGENT REGISTRY","Agents","＋ 注册 Agent","agent"],
  squads:["COLLABORATION","Squads","＋ 组装 Squad","squad"],
  runtimes:["INFRASTRUCTURE","运行环境","↻ 探测本机","runtime"],
};
const state = {route:"board",issues:[],tasks:[],agents:[],squads:[],runtimes:[],capabilities:[],projects:[],projectCandidates:[],search:"",selectedIssue:null,sockets:[]};

function toast(message, error = false) {
  const item = document.createElement("div"); item.className = `toast${error ? " error" : ""}`; item.textContent = message;
  $("#toast-region").append(item); setTimeout(() => item.remove(), 3600);
}
function formatTime(value) { if (!value) return "—"; return new Intl.DateTimeFormat("zh-CN", {month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); }
function duration(task) { const seconds = Math.max(0, (new Date(task.updated_at)-new Date(task.created_at))/1000); return seconds < 60 ? `${Math.round(seconds)} 秒` : `${Math.round(seconds/60)} 分钟`; }
function agentById(id) { return state.agents.find(item => item.id === id); }
function squadById(id) { return state.squads.find(item => item.id === id); }
function assignee(issue) {
  if (issue.assignee_type === "squad") { const squad = squadById(issue.assignee_id); return {name:squad?.name || issue.assignee_id,type:"squad",provider:"squad"}; }
  const agent = agentById(issue.assignee_id); const provider = agent?.provider || "agent";
  return {name:agent?.name || issue.assignee_id,type:provider === "a2a" ? "remote" : "local",provider};
}
function tasksFor(issueId) { return state.tasks.filter(task => task.issue_id === issueId).sort((a,b) => new Date(b.created_at)-new Date(a.created_at)); }
function badge(text, type="neutral") { return `<span class="badge ${type}">${esc(text)}</span>`; }

async function loadData() {
  const [issues,tasks,agents,squads,runtimes,capabilities,projects,projectCandidates] = await Promise.all(["/api/issues","/api/tasks","/api/agents","/api/squads?workspace_id=local","/api/runtimes","/api/runtime-capabilities","/api/projects?workspace_id=local","/api/project-candidates"].map(url => api(url)));
  Object.assign(state,{issues,tasks,agents,squads,runtimes,capabilities,projects,projectCandidates});
}
async function checkHealth() {
  try { await api("/health/ready"); $("#health-dot").className="health-dot ready"; $("#health-label").textContent="服务已就绪"; }
  catch (error) { $("#health-dot").className="health-dot error"; $("#health-label").textContent="服务未就绪"; }
}
function setRoute() {
  const candidate = location.hash.slice(1); state.route = routeMeta[candidate] ? candidate : "board";
  const [eyebrow,title,action] = routeMeta[state.route]; $("#page-eyebrow").textContent=eyebrow; $("#page-title").textContent=title; $("#primary-action").textContent=action;
  $$('[data-route]').forEach(link => link.classList.toggle("active", link.dataset.route === state.route)); render();
}
function render() { ({board:renderBoard,agents:renderAgents,squads:renderSquads,runtimes:renderRuntimeNodes})[state.route](); }

function issueCard(issue) {
  const owner=assignee(issue), tasks=tasksFor(issue.id), latest=tasks[0], waiting=tasks.some(t=>t.status==="waiting_input"), failed=latest?.status==="failed";
  const squadTasks=tasks.filter(t=>!t.is_leader_task), finished=squadTasks.filter(t=>["completed","failed","cancelled"].includes(t.status)).length;
  const note=waiting?`<div class="status-note">Agent 正在等待用户补充信息</div>`:failed?`<div class="status-note error">${esc(latest.error||"最近一次执行失败")}</div>`:"";
  const progress=owner.type==="squad"&&squadTasks.length?`<div class="progress" title="${finished}/${squadTasks.length} 个成员任务已结束"><span style="width:${finished/squadTasks.length*100}%"></span></div>`:"";
  return `<article class="issue-card" draggable="true" data-issue-id="${esc(issue.id)}" data-status="${esc(issue.status)}">
    <div class="card-top">${badge(owner.type==="squad"?"SQUAD":owner.type==="remote"?"REMOTE":"LOCAL",owner.type)}${latest?badge(TASK_LABEL[latest.status]||latest.status,waiting?"warning":failed?"danger":"neutral"):""}</div>
    <h3 class="card-title">${esc(issue.title)}</h3>${note}${progress}
    <div class="card-foot"><span class="assignee">${esc(owner.name)}</span><span>${formatTime(issue.updated_at)}</span></div>
  </article>`;
}
function renderBoard() {
  const visible=state.issues.filter(i=>i.status!=="cancelled"&&(!state.search||`${i.title} ${assignee(i).name}`.toLowerCase().includes(state.search.toLowerCase())));
  const active=state.tasks.filter(t=>ACTIVE_TASKS.has(t.status)).length, waiting=state.tasks.filter(t=>t.status==="waiting_input").length;
  $("#page").innerHTML=`<div class="board-toolbar"><div class="stats"><span class="stat-chip"><strong>${visible.length}</strong>任务</span><span class="stat-chip"><strong>${active}</strong>活动执行</span><span class="stat-chip"><strong>${waiting}</strong>等待输入</span></div><div class="search-box"><input id="board-search" value="${esc(state.search)}" placeholder="搜索任务或负责人"></div></div>
    <div class="board">${ISSUE_COLUMNS.map(([status,label])=>{const items=visible.filter(i=>i.status===status);return `<section class="board-column" data-drop-status="${status}"><header class="column-head"><span class="column-title"><i class="column-dot ${status}"></i>${label}</span><span class="column-count">${items.length}</span></header><div class="column-body">${items.map(issueCard).join("")||'<div class="empty-column">暂无任务</div>'}</div></section>`}).join("")}</div>`;
  $("#board-search").addEventListener("input", event=>{state.search=event.target.value;renderBoard();$("#board-search").focus();});
  setupBoardEvents();
}
function setupBoardEvents() {
  $$(".empty-column").forEach(emptyColumn => {
    emptyColumn.textContent = "＋ 在此创建任务";
    emptyColumn.setAttribute("role", "button");
    emptyColumn.tabIndex = 0;
    emptyColumn.addEventListener("click", showCreateIssue);
    emptyColumn.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") showCreateIssue();
    });
  });
  $$(".issue-card").forEach(card=>{
    card.addEventListener("click",()=>openIssue(card.dataset.issueId));
    card.addEventListener("dragstart",event=>{card.classList.add("dragging");event.dataTransfer.setData("text/plain",card.dataset.issueId);});
    card.addEventListener("dragend",()=>card.classList.remove("dragging"));
  });
  $$(".board-column").forEach(column=>{
    column.addEventListener("dragover",event=>{event.preventDefault();column.classList.add("drag-over")});
    column.addEventListener("dragleave",()=>column.classList.remove("drag-over"));
    column.addEventListener("drop",async event=>{event.preventDefault();column.classList.remove("drag-over");await moveIssue(event.dataTransfer.getData("text/plain"),column.dataset.dropStatus);});
  });
}
async function moveIssue(id,target) {
  const issue=state.issues.find(i=>i.id===id); if(!issue||issue.status===target)return;
  if(!(USER_MOVES[issue.status]||[]).includes(target)){toast(`不能直接从“${ISSUE_LABEL[issue.status]}”移动到“${ISSUE_LABEL[target]}”`,true);return;}
  try{await api(`/api/issues/${id}/status`,jsonOptions("PATCH",{status:target}));await refresh();toast(`任务已移至“${ISSUE_LABEL[target]}”`);}catch(error){toast(error.message,true);}
}

function renderAgents() {
  $("#page").innerHTML=`<div class="content-header"><div><h2>Agent 注册表</h2><p>统一管理本地 Coding Agent、ACP Agent 与 A2A 业务 Agent。</p></div></div>${state.agents.length?`<div class="card-grid">${state.agents.map(agent=>{const remote=agent.provider==="a2a";return `<article class="entity-card clickable" data-agent-id="${esc(agent.id)}"><div class="entity-head"><div><h3 class="entity-name">${esc(agent.name)}</h3><div class="entity-id">${esc(agent.id)}</div></div>${badge(remote?"REMOTE":"LOCAL",remote?"remote":"local")}</div><p class="entity-description">${esc(agent.instructions||"尚未配置 Agent 指令")}</p><div class="meta-list"><div class="meta-row"><span>Provider</span><span>${esc(agent.provider.toUpperCase())}</span></div><div class="meta-row"><span>Runtime</span><span>${esc(state.runtimes.find(r=>r.id===agent.runtime_id)?.name||agent.runtime_id)}</span></div><div class="meta-row"><span>配置</span><span>${esc(configSummary(agent))}</span></div></div></article>`}).join("")}</div>`:empty("还没有 Agent","先注册一个本地或远程 Agent。")}`;
  $$("[data-agent-id]").forEach(card => card.onclick = () => showAgentForm(agentById(card.dataset.agentId)));
}
function configSummary(agent){const c=agent.provider_config||{};if(agent.provider==="a2a")return c.skill_id||c.agent_card_url||"未指定 Card";if(agent.provider==="acp")return (c.command||[]).join(" ")||"未指定命令";return c.model||c.executable||"使用默认配置";}
function renderSquads() {
  $("#page").innerHTML=`<div class="content-header"><div><h2>协作小队</h2><p>由 Leader 理解目标、选择成员并汇总多个 Agent 的结果。</p></div></div>${state.squads.length?`<div class="card-grid">${state.squads.map(s=>`<article class="entity-card clickable" data-squad-id="${esc(s.id)}"><div class="entity-head"><div><h3 class="entity-name">${esc(s.name)}</h3><div class="entity-id">${esc(s.id)}</div></div>${badge(`${s.members.length} MEMBERS`,"squad")}</div><p class="entity-description">${esc(s.description||s.instructions||"尚未填写协作说明")}</p><div class="meta-list"><div class="meta-row"><span>Leader</span><span>${esc(agentById(s.leader_agent_id)?.name||s.leader_agent_id)}</span></div><div class="meta-row"><span>成员构成</span><span>${s.members.filter(m=>agentById(m.agent_id)?.provider==="a2a").length} 远程 / ${s.members.filter(m=>agentById(m.agent_id)?.provider!=="a2a").length} 本地</span></div></div></article>`).join("")}</div>`:empty("还没有 Squad","选择一个 Leader 和若干成员组装协作小队。")}`;
  $$('[data-squad-id]').forEach(card=>card.onclick=()=>openSquad(card.dataset.squadId));
}
function empty(title,detail){return `<div class="empty-state"><div><strong>${title}</strong>${detail}</div></div>`;}

async function openIssue(id) {
  closeSockets(); state.selectedIssue=id; openDrawer("<div class='loading-state'><span class='spinner'></span>加载任务详情…</div>");
  try { const [issue,tasks]=await Promise.all([api(`/api/issues/${id}`),api(`/api/tasks?issue_id=${encodeURIComponent(id)}`)]); renderIssueDetail(issue,tasks); watchTasks(tasks); }
  catch(error){toast(error.message,true);closeDrawer();}
}
function renderIssueDetail(issue,tasks) {
  const owner=assignee(issue), comments=issue.comments||[], sorted=[...tasks].sort((a,b)=>new Date(b.created_at)-new Date(a.created_at));
  const action=(label,status,kind="secondary-button")=>`<button class="${kind} small-button" data-status-action="${status}">${label}</button>`;
  let actions=""; if(issue.status==="in_review")actions=action("要求返工","in_progress")+action("标记阻塞","blocked")+action("确认完成","done","primary-button");
  if(issue.status==="blocked")actions=action("重新排队","todo")+action("恢复执行","in_progress");
  if(!["done","cancelled"].includes(issue.status))actions+=action("取消任务","cancelled","danger-button");
  const executionMode=issue.project_path?(issue.project_execution_mode==="in_place"?"当前目录":"独立 Worktree"):"独立空工作目录";
  const commentById=new Map(comments.map(comment=>[comment.id,comment]));
  const authorName=comment=>comment.author_type==="agent"?(agentById(comment.author_id)?.name||comment.author_id):comment.author_id;
  const timeline=comments.map(c=>{const parent=c.parent_comment_id?commentById.get(c.parent_comment_id):null;return `<article class="timeline-item${c.parent_comment_id?" timeline-reply":""}"><div class="timeline-meta"><span>${esc(authorName(c))}${parent?` · 回复 ${esc(authorName(parent))}`:""}</span><span>${formatTime(c.created_at)}</span></div><div class="prose">${esc(c.body)}</div><div class="comment-actions"><button class="text-button" data-reply-comment="${esc(c.id)}" data-reply-author="${esc(authorName(c))}">回复</button></div></article>`;}).join("");
  const commentForm=!["done","cancelled","backlog"].includes(issue.status)?`<form class="comment-form" id="comment-form"><input type="hidden" name="parent_comment_id"><div class="comment-compose"><div class="reply-context"><span class="field-hint" id="reply-target-hint">新评论默认发送给当前负责人；也可输入 @agent:ID 或 @squad:ID</span><button type="button" class="text-button" id="clear-reply-target" hidden>取消回复</button></div><textarea name="body" required placeholder="补充上下文、回答 Agent 或提出修改要求…"></textarea></div><button class="primary-button">发送</button></form>`:"";
  $("#drawer-content").innerHTML=`<header class="drawer-header"><div>${badge(ISSUE_LABEL[issue.status]||issue.status,issue.status==="blocked"?"danger":issue.status==="in_review"?"warning":"neutral")}<h2>${esc(issue.title)}</h2></div><button class="icon-button" data-close-drawer>×</button></header><div class="drawer-body"><div class="detail-grid"><div><section class="detail-section"><h3 class="section-title">任务目标</h3><div class="prose">${esc(issue.body)}</div></section><section class="detail-section"><h3 class="section-title">讨论与追问 <span>${comments.length}</span></h3><div class="timeline"><article class="timeline-item"><div class="timeline-meta"><span>任务创建</span><span>${formatTime(issue.created_at)}</span></div><div class="prose">${esc(issue.body)}</div></article>${timeline}</div>${commentForm}</section></div><aside><section class="detail-section"><h3 class="section-title">任务信息</h3><div class="detail-facts"><div class="meta-row"><span>负责人</span><span>${esc(owner.name)}</span></div><div class="meta-row"><span>执行类型</span><span>${owner.type==="squad"?"Squad":owner.type==="remote"?"远程 Agent":"本地 Agent"}</span></div><div class="meta-row"><span>Provider</span><span>${esc(owner.provider)}</span></div><div class="meta-row"><span>项目路径</span><span>${esc(issue.project_path||"—")}</span></div><div class="meta-row"><span>目录策略</span><span>${executionMode}</span></div><div class="meta-row"><span>更新时间</span><span>${formatTime(issue.updated_at)}</span></div></div><div class="detail-actions">${actions}</div></section><section class="detail-section"><h3 class="section-title">执行记录 <span>${sorted.length}</span></h3><div class="execution-list">${sorted.map(executionCard).join("")||'<span class="field-hint">尚未生成执行记录</span>'}</div></section></aside></div></div>`;
  $('[data-close-drawer]').onclick=closeDrawer;
  $$('[data-status-action]').forEach(button=>button.onclick=()=>changeIssueStatus(issue.id,button.dataset.statusAction));
  $("#comment-form")?.addEventListener("submit",event=>submitComment(event,issue.id));
  $("#clear-reply-target")?.addEventListener("click",clearCommentReply);
  $$('[data-reply-comment]').forEach(button=>button.onclick=()=>prepareCommentReply(button.dataset.replyComment,button.dataset.replyAuthor));
  $$('[data-task-events]').forEach(detail=>detail.addEventListener("toggle",()=>detail.open&&loadEvents(detail.dataset.taskEvents)));
  $$('[data-cancel-task]').forEach(button=>button.onclick=()=>cancelTask(button.dataset.cancelTask));
}
function executionCard(task) {
  const agent=agentById(task.agent_id), cancellable=ACTIVE_TASKS.has(task.status);
  const inputCount=(task.delivered_comment_ids||task.merged_comment_ids||[]).length;
  const actions=cancellable?`<div class="detail-actions"><button class="danger-button small-button" data-cancel-task="${esc(task.id)}">取消执行</button></div>`:"";
  return `<details class="execution-card" data-task-events="${esc(task.id)}"><summary><div class="execution-title"><strong>${esc(agent?.name||task.agent_id)}</strong>${badge(TASK_LABEL[task.status]||task.status,task.status==="failed"?"danger":task.status==="waiting_input"?"warning":"neutral")}</div><div class="execution-meta">${task.is_leader_task?badge("LEADER","squad"):task.squad_id?badge("MEMBER","local"):""}${badge(agent?.provider?.toUpperCase()||"AGENT",agent?.provider==="a2a"?"remote":"local")}${inputCount?badge(`合并 ${inputCount} 条输入`,"neutral"):""}${badge(`尝试 ${task.attempt}`,"neutral")}</div></summary>${task.handoff_note?`<div class="execution-output">交接：${esc(task.handoff_note)}</div>`:""}${task.output||task.error?`<div class="execution-output">${esc(task.output||task.error)}</div>`:""}${actions}<div class="event-list" id="events-${esc(task.id)}"><span class="field-hint">展开后加载事件 · ${duration(task)}</span></div></details>`;
}
function compactTimelineEvents(events){return events.reduce((items,event)=>{const previous=items[items.length-1];if(previous?.event_type==="agent.text"&&event.event_type==="agent.text"){previous.payload.text=`${previous.payload.text||""}${event.payload.text||""}`;return items;}items.push({...event,payload:{...event.payload}});return items;},[]);}
async function loadEvents(taskId){const root=$(`#events-${CSS.escape(taskId)}`);if(!root||root.dataset.loaded)return;root.dataset.loaded="1";try{const events=compactTimelineEvents(await api(`/api/tasks/${taskId}/events?include_internal=false&limit=200`));root.innerHTML=`<span class="field-hint">仅展示最近 200 条有效事件</span>${events.map(e=>`<div class="event-row"><span>${esc(e.event_type)}</span><code>${esc(JSON.stringify(e.payload))}</code></div>`).join("")||'<span class="field-hint">暂无事件</span>'}`;}catch(error){root.textContent=error.message;}}
function prepareCommentReply(commentId,authorName){
  const form=$("#comment-form");
  if(!form)return;
  form.elements.parent_comment_id.value=commentId;
  $("#reply-target-hint").textContent=`正在回复 ${authorName||"该评论"}；系统会根据评论上下文确定 Agent`;
  $("#clear-reply-target").hidden=false;
  form.elements.body.focus();
}
function clearCommentReply(){const form=$("#comment-form");if(!form)return;form.elements.parent_comment_id.value="";$("#reply-target-hint").textContent="新评论默认发送给当前负责人；也可输入 @agent:ID 或 @squad:ID";$("#clear-reply-target").hidden=true;}
async function submitComment(event,id){event.preventDefault();const data=new FormData(event.target),body=data.get("body").trim(),parentCommentId=data.get("parent_comment_id")||null;if(!body)return;try{await api(`/api/issues/${id}/comments`,jsonOptions("POST",{body,author_id:"local-user",parent_comment_id:parentCommentId}));await refresh();await openIssue(id);toast(parentCommentId?"回复已发送":"补充内容已发送");}catch(error){toast(error.message,true);}}
async function changeIssueStatus(id,status){try{await api(`/api/issues/${id}/status`,jsonOptions("PATCH",{status}));await refresh();if(status==="cancelled")closeDrawer();else await openIssue(id);toast(`状态已更新为“${ISSUE_LABEL[status]}”`);}catch(error){toast(error.message,true);}}
async function cancelTask(id){try{await api(`/api/tasks/${id}/cancel`,{method:"POST"});await refresh();await openIssue(state.selectedIssue);toast("已请求取消执行");}catch(error){toast(error.message,true);}}
function watchTasks(tasks){tasks.filter(t=>ACTIVE_TASKS.has(t.status)).forEach(task=>{const protocol=location.protocol==="https:"?"wss":"ws";const socket=new WebSocket(`${protocol}://${location.host}/api/tasks/${task.id}/events`);socket.onmessage=()=>scheduleLiveRefresh();state.sockets.push(socket);});}
let liveTimer;function scheduleLiveRefresh(){clearTimeout(liveTimer);liveTimer=setTimeout(async()=>{const id=state.selectedIssue;await refresh();if(id)openIssue(id);},500)}
function closeSockets(){state.sockets.forEach(socket=>socket.close());state.sockets=[];}
function openDrawer(html){$("#drawer-content").innerHTML=html;$("#drawer-backdrop").hidden=false;$("#drawer").classList.add("open");$("#drawer").setAttribute("aria-hidden","false");}
function closeDrawer(){closeSockets();state.selectedIssue=null;$("#drawer").classList.remove("open");$("#drawer").setAttribute("aria-hidden","true");setTimeout(()=>$("#drawer-backdrop").hidden=true,220);}

function modal(title,subtitle,body,wide=false){$(".modal").classList.toggle("wide",wide);$("#modal-content").innerHTML=`<header class="modal-header"><div><h2 id="modal-title">${title}</h2><p>${subtitle}</p></div><button class="icon-button" data-close-modal>×</button></header><div class="modal-body">${body}</div>`;$("#modal-backdrop").hidden=false;$('[data-close-modal]').onclick=closeModal;}
function closeModal(){$("#modal-backdrop").hidden=true;}
function field(label,name,opts={}){const tag=opts.textarea?`<textarea name="${name}" ${opts.required?"required":""} ${opts.readonly?"readonly":""} placeholder="${esc(opts.placeholder||"")}">${esc(opts.value||"")}</textarea>`:`<input name="${name}" type="${opts.type||"text"}" ${opts.required?"required":""} ${opts.readonly?"readonly":""} value="${esc(opts.value||"")}" placeholder="${esc(opts.placeholder||"")}">`;return `<div class="field ${opts.full?"full":""}"><label>${label}</label>${tag}${opts.hint?`<span class="field-hint">${opts.hint}</span>`:""}</div>`;}
function targetOptions(){return `<optgroup label="本地 Agent">${state.agents.filter(a=>a.provider!=="a2a").map(a=>`<option value="agent|${esc(a.id)}">${esc(a.name)} · ${esc(a.provider)}</option>`).join("")}</optgroup><optgroup label="远程业务 Agent">${state.agents.filter(a=>a.provider==="a2a").map(a=>`<option value="agent|${esc(a.id)}">${esc(a.name)} · A2A</option>`).join("")}</optgroup><optgroup label="Agent Squad">${state.squads.map(s=>`<option value="squad|${esc(s.id)}">${esc(s.name)} · ${s.members.length} 人</option>`).join("")}</optgroup>`;}
function projectOptions(runtimeId) {
  const projects = state.projects.filter(project => project.runtime_id === runtimeId);
  const registeredPaths = new Set(projects.map(project => project.local_path));
  const registered = projects.map(project => ({value:`project|${project.id}`, label:`${project.name} · ${project.execution_mode==="in_place"?"当前目录":"Worktree"}`}));
  const discovered = state.projectCandidates.filter(candidate => !registeredPaths.has(candidate.local_path)).map(candidate => ({value:`candidate|${candidate.local_path}`, label:`${candidate.name} · 本机发现`}));
  const options = [...registered, ...discovered];
  const recent = localStorage.getItem("mica.recentProject") || "";
  return `<option value="">不关联项目</option>${options.map(item => `<option value="${esc(item.value)}"${item.value===recent?" selected":""}>${esc(item.label)}</option>`).join("")}`;
}
function targetAgent(target) {
  const [type,id] = target.split("|");
  if (type === "agent") return agentById(id);
  return agentById(squadById(id)?.leader_agent_id);
}
function targetRuntimeId(target) {
  return targetAgent(target)?.runtime_id || state.runtimes[0]?.id;
}
function targetSupportsProject(target) {
  return ["codex","claude","pi","acp"].includes(targetAgent(target)?.provider);
}
function showCreateIssue(){
  if(!state.agents.length){toast("请先注册 Agent",true);location.hash="#agents";return;}
  modal("新建任务","项目是可选上下文；关联项目时可选择直接运行或使用隔离 Worktree。",`<form id="create-issue" class="form-grid">${field("任务标题","title",{required:true,placeholder:"例如：排查订单同步失败"})}<div class="field"><label>执行方</label><select name="target" id="issue-target">${targetOptions()}</select></div>${field("任务描述","body",{required:true,textarea:true,full:true,placeholder:"说明背景、期望结果和必要上下文"})}<div class="field"><label>项目</label><select name="project" id="issue-project"></select><span class="field-hint" id="project-hint"></span></div><div class="field"><label>项目执行方式</label><select name="execution_mode" id="issue-execution-mode"><option value="worktree">独立 Worktree</option><option value="in_place">当前目录</option></select><span class="field-hint" id="execution-mode-hint"></span></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">创建并执行</button></div></form>`);
  $$("[data-close-modal]").forEach(button=>button.onclick=closeModal);
  const updateProjectHints = () => {
    const target=$("#issue-target").value;
    const project=$("#issue-project");
    const mode=$("#issue-execution-mode");
    const supported=targetSupportsProject(target);
    project.disabled=!supported;
    if(!supported) project.value="";
    const selected=project.value;
    mode.disabled=!supported||!selected;
    $("#project-hint").textContent=!supported
      ? "远程业务 Agent 不访问本机项目。"
      : selected?"所选项目将作为 Agent 的工作目录。":"不关联项目时，Agent 在独立的空工作目录中执行。";
    $("#execution-mode-hint").textContent=!selected
      ? "选择项目后可配置。"
      : mode.value==="in_place"?"直接操作当前目录；同一路径任务串行执行，修改会立即出现在工作区。":"每个任务使用隔离 Worktree，可并发执行并保留独立分支。";
  };
  const selectProjectMode = () => {
    const selected=$("#issue-project").value;
    const [kind,id]=selected.split("|");
    const registered=kind==="project"?state.projects.find(item=>item.id===id):null;
    $("#issue-execution-mode").value=registered?.execution_mode||"worktree";
    updateProjectHints();
  };
  const loadProjectOptions = () => {
    const target=$("#issue-target").value;
    $("#issue-project").innerHTML=projectOptions(targetRuntimeId(target));
    selectProjectMode();
  };
  $("#issue-target").onchange=loadProjectOptions;
  $("#issue-project").onchange=selectProjectMode;
  $("#issue-execution-mode").onchange=updateProjectHints;
  loadProjectOptions();
  $("#create-issue").onsubmit=async event=>{
    event.preventDefault();
    const data=Object.fromEntries(new FormData(event.target));
    const [assignee_type,assignee_id]=data.target.split("|");
    try{
      let projectId=null;
      if(data.project){
        const separator=data.project.indexOf("|");
        const kind=data.project.slice(0,separator),value=data.project.slice(separator+1);
        if(kind==="project") projectId=value;
        else {
          const candidate=state.projectCandidates.find(item=>item.local_path===value);
          if(!candidate) throw new Error("所选项目已不在扫描结果中，请刷新后重试");
          const project=await api("/api/projects",jsonOptions("POST",{name:candidate.name,local_path:candidate.local_path,workspace_id:"local",runtime_id:targetRuntimeId(data.target),execution_mode:data.execution_mode}));
          projectId=project.id;
        }
        if(kind==="project") {
          const existing=state.projects.find(item=>item.id===projectId);
          if(existing&&existing.execution_mode!==data.execution_mode) await api(`/api/projects/${projectId}`,jsonOptions("PATCH",{execution_mode:data.execution_mode}));
        }
        localStorage.setItem("mica.recentProject",data.project);
      }
      const issue=await api("/api/issues",{...jsonOptions("POST",{title:data.title,body:data.body,workspace_id:"local",assignee_type,assignee_id,project_id:projectId}),headers:{"Content-Type":"application/json","Idempotency-Key":crypto.randomUUID()}});
      closeModal();await refresh();openIssue(issue.id);toast("任务已创建");
    }catch(error){toast(error.message,true);}
  };
}

function memberRow(){return `<div class="member-row"><select name="member_agent"><option value="">选择成员</option>${state.agents.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`)}</select><input name="member_role" placeholder="职责"><input name="member_caps" placeholder="能力，逗号分隔"><button type="button" class="icon-button" data-remove-row>×</button></div>`;}
function showCreateSquad(){if(state.agents.length<2){toast("至少注册两个 Agent 后才能组装 Squad",true);return;}modal("组装 Agent Squad","Leader 负责理解目标、委派成员并汇总结果。",`<form id="create-squad" class="form-grid">${field("Squad 名称","name",{required:true,placeholder:"空间方案小队"})}<div class="field"><label>Leader Agent</label><select name="leader_agent_id">${state.agents.map(a=>`<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.provider)}</option>`)}</select></div>${field("业务场景","description",{textarea:true,full:true,placeholder:"这个 Squad 解决什么问题"})}${field("协作规则","instructions",{textarea:true,full:true,placeholder:"例如：先进行几何校验，再生成摆放方案，最后由 Leader 汇总。"})}<div class="field full"><label>成员与职责</label><div class="member-editor" id="member-editor">${memberRow()}</div><button type="button" class="secondary-button small-button" id="add-member">＋ 添加成员</button></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">创建 Squad</button></div></form>`,true);const bindRows=()=>$$('[data-remove-row]').forEach(b=>b.onclick=()=>b.closest('.member-row').remove());bindRows();$("#add-member").onclick=()=>{$("#member-editor").insertAdjacentHTML("beforeend",memberRow());bindRows();};$$('[data-close-modal]').forEach(b=>b.onclick=closeModal);$("#create-squad").onsubmit=async event=>{event.preventDefault();const form=event.target,data=new FormData(form),agents=data.getAll("member_agent"),roles=data.getAll("member_role"),caps=data.getAll("member_caps");const members=agents.map((id,i)=>({agent_id:id,role:roles[i],capabilities:caps[i].split(/[,，]/).map(x=>x.trim()).filter(Boolean)})).filter(x=>x.agent_id&&x.agent_id!==data.get("leader_agent_id"));try{await api("/api/squads",jsonOptions("POST",{name:data.get("name"),workspace_id:"local",leader_agent_id:data.get("leader_agent_id"),description:data.get("description"),instructions:data.get("instructions"),members}));closeModal();await refresh();toast("Squad 已创建");}catch(error){toast(error.message,true);}};}
function openSquad(id){const s=squadById(id);if(!s)return;openDrawer(`<header class="drawer-header"><div>${badge("SQUAD","squad")}<h2>${esc(s.name)}</h2></div><button class="icon-button" data-close-drawer>×</button></header><div class="drawer-body"><section class="detail-section"><h3 class="section-title">业务场景</h3><div class="prose">${esc(s.description||"尚未填写")}</div></section><section class="detail-section"><h3 class="section-title">Leader 协作规则</h3><div class="prose">${esc(s.instructions||"尚未填写")}</div></section><section class="detail-section"><h3 class="section-title">成员 <span>${s.members.length}</span></h3><div class="member-list">${s.members.map(m=>{const a=agentById(m.agent_id),leader=m.agent_id===s.leader_agent_id;return `<div class="member-item"><div><strong>${esc(a?.name||m.agent_id)} ${leader?badge("LEADER","squad"):""}</strong><p>${esc(m.role||"未指定职责")} · ${(m.capabilities||[]).map(esc).join("、")||"未填写能力"} · ${esc(a?.provider||"")}</p></div>${!leader?`<button class="danger-button small-button" data-remove-member="${esc(m.agent_id)}">移除</button>`:""}</div>`}).join("")}</div></section></div>`);$('[data-close-drawer]').onclick=closeDrawer;$$('[data-remove-member]').forEach(b=>b.onclick=async()=>{try{await api(`/api/squads/${id}/members/${b.dataset.removeMember}`,{method:"DELETE"});await refresh();openSquad(id);toast("成员已移除");}catch(error){toast(error.message,true);}});}
function probeConfiguredRuntime() {
  const runtime = state.runtimes[0];
  if (!runtime) { toast("尚未发现正在运行的 Daemon", true); return; }
  probeRuntime(runtime.id);
}

function capabilitiesFor(runtimeId) {
  return state.capabilities.filter(item => item.runtime_id === runtimeId);
}
function renderRuntimeNodes() {
  $("#page").innerHTML = `<div class="content-header"><div><h2>本地执行节点</h2><p>Daemon 自动发现本机可用的 Agent CLI；Agent 只能绑定到已发现的 Provider。</p></div></div>${state.runtimes.length ? `<div class="card-grid">${state.runtimes.map(runtime => {
    const caps = capabilitiesFor(runtime.id);
    const providers = caps.length ? caps.map(capability => `<div class="member-item"><div><strong>${esc(capability.provider.toUpperCase())} ${badge(capability.status === "available" ? "可用" : "异常", capability.status === "available" ? "local" : "danger")}</strong><p>${esc(capability.executable_path)} · ${esc(capability.version || "版本未知")}${capability.error ? ` · ${esc(capability.error)}` : ""}</p></div></div>`).join("") : '<div class="field-hint">尚未发现 Codex、Claude Code 或 Pi</div>';
    return `<article class="entity-card"><div class="entity-head"><div><h3 class="entity-name">${esc(runtime.name)}</h3><div class="entity-id">${esc(runtime.hostname || runtime.id)} · ${esc(runtime.daemon_id)}</div></div>${badge(runtime.status === "online" ? "ONLINE" : "OFFLINE", runtime.status === "online" ? "local" : "danger")}</div><p class="entity-description">${esc(runtime.platform || "等待 Daemon 上报系统信息")}</p><div class="meta-list"><div class="meta-row"><span>最大并发 Slot</span><span>${runtime.max_slots}</span></div><div class="meta-row"><span>发现的 Provider</span><span>${caps.filter(item => item.status === "available").length}</span></div><div class="meta-row"><span>最近探测</span><span>${formatTime(runtime.last_seen_at)}</span></div></div><div class="member-list" style="margin-top:14px">${providers}</div><div class="detail-actions"><button class="secondary-button small-button" data-edit-runtime="${esc(runtime.id)}">编辑节点</button><button class="primary-button small-button" data-probe-runtime="${esc(runtime.id)}">重新探测</button></div></article>`;
  }).join("")}</div>` : empty("还没有 Runtime", "添加执行节点后才能发现本地 Agent CLI。")}`;
  $$('[data-edit-runtime]').forEach(button => button.onclick = () => showEditRuntime(button.dataset.editRuntime));
  $$('[data-probe-runtime]').forEach(button => button.onclick = () => probeRuntime(button.dataset.probeRuntime));
}
function showEditRuntime(id) {
  const runtime = state.runtimes.find(item => item.id === id);
  if (!runtime) return;
  modal("编辑执行节点", "Runtime ID 和 Daemon 归属不可修改。", `<form id="edit-runtime" class="form-grid">${field("显示名称", "name", {required:true, value:runtime.name})}${field("最大并发 Slot", "max_slots", {required:true, type:"number", value:String(runtime.max_slots), hint:"只影响后续任务的并发领取。"})}<div class="field full"><label>Runtime ID</label><input value="${esc(runtime.id)}" disabled></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">保存修改</button></div></form>`);
  $$('[data-close-modal]').forEach(button => button.onclick = closeModal);
  $("#edit-runtime").onsubmit = async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.target)); try { await api(`/api/runtimes/${id}`, jsonOptions("PATCH", {name:data.name, max_slots:Number(data.max_slots)})); closeModal(); await refresh(); toast("执行节点已更新"); } catch (error) { toast(error.message, true); } };
}
async function probeRuntime(id) {
  try { toast("正在探测本机 Agent CLI…"); await api(`/api/runtimes/${id}/probe`, {method:"POST"}); await refresh(); toast("Runtime 探测完成"); }
  catch (error) { toast(error.message, true); }
}
function runtimeProviderOptions(runtimeId, selectedProvider = "") {
  const selected = provider => provider === selectedProvider ? " selected" : "";
  const discovered = capabilitiesFor(runtimeId).filter(item => item.status === "available");
  let local = discovered.map(item => `<option value="${esc(item.provider)}"${selected(item.provider)}>${esc(item.provider.toUpperCase())} · 已发现</option>`).join("");
  if (selectedProvider && ["codex", "claude", "pi"].includes(selectedProvider) && !discovered.some(item => item.provider === selectedProvider)) {
    local += `<option value="${esc(selectedProvider)}" selected disabled>${esc(selectedProvider.toUpperCase())} · 当前不可用</option>`;
  }
  return `<optgroup label="本地已发现">${local || '<option disabled>没有发现本地 CLI</option>'}</optgroup><optgroup label="协议接入"><option value="acp"${selected("acp")}>ACP · 手动命令</option><option value="a2a"${selected("a2a")}>A2A · 远程 Agent</option></optgroup><option value="stub"${selected("stub")}>Stub · 仅测试</option>`;
}
function showAgentForm(agent = null) {
  if (!state.runtimes.length) { toast("请先启动本机 Daemon", true); location.hash="#runtimes"; return; }
  const editing = Boolean(agent);
  const selectedRuntime = agent?.runtime_id || state.runtimes[0].id;
  modal(editing ? "编辑 Agent" : "注册 Agent", "先选择执行节点，再从该节点发现的 Provider 中选择运行方式。", `<form id="agent-form" class="form-grid">${field("Agent ID","id",{required:true,readonly:editing,value:agent?.id||"",placeholder:"geometry-agent"})}${field("显示名称","name",{required:true,value:agent?.name||"",placeholder:"Geometry Agent"})}<div class="field"><label>执行节点</label><select name="runtime_id" id="agent-runtime">${state.runtimes.map(runtime => `<option value="${esc(runtime.id)}"${runtime.id===selectedRuntime?" selected":""}>${esc(runtime.name)}</option>`).join("")}</select></div><div class="field"><label>Runtime / Provider</label><select name="provider" id="agent-provider"></select></div>${field("Agent 指令","instructions",{textarea:true,full:true,value:agent?.instructions||"",placeholder:"描述 Agent 的职责、边界与输出要求"})}<div class="field full" id="provider-fields-v2"></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">${editing?"保存修改":"保存 Agent"}</button></div></form>`, true);
  const renderProviderFields = () => {
    const provider=$("#agent-provider").value;
    const root=$("#provider-fields-v2");
    const config=provider===agent?.provider ? (agent.provider_config||{}) : {};
    if(provider==="a2a") root.innerHTML=`${field("Agent Card URL","agent_card_url",{required:true,value:config.agent_card_url||"",placeholder:"https://agent.internal/.well-known/agent-card.json"})}${field("Skill ID","skill_id",{value:config.skill_id||"",placeholder:"consultation"})}${field("Token 环境变量","token_env",{value:config.token_env||"",placeholder:"A2A_API_TOKEN"})}${field("输出 JSON Schema","output_schema",{textarea:true,value:config.output_schema?JSON.stringify(config.output_schema,null,2):"",placeholder:'{"type":"object"}'})}`;
    else if(provider==="acp") root.innerHTML=field("ACP 启动命令","command",{required:true,value:(config.command||[]).join(" "),placeholder:"my-agent --acp",hint:"ACP 无统一可执行文件名，需要显式配置。"});
    else if(provider==="claude") root.innerHTML=`${field("模型（可选）","model",{value:config.model||"",placeholder:"使用 Runtime 默认模型"})}<div class="field"><label>执行权限</label><div class="field-hint">无人值守模式：平台自动批准 Claude 的工具调用，并禁止脱离 Daemon 管理的后台任务。关联真实项目时，优先使用独立 Worktree。</div></div>`;
    else root.innerHTML=field("模型（可选）","model",{value:config.model||"",placeholder:"使用 Runtime 默认模型"});
  };
  const renderProvider = preferred => { $("#agent-provider").innerHTML=runtimeProviderOptions($("#agent-runtime").value, preferred); renderProviderFields(); };
  $("#agent-runtime").onchange=()=>renderProvider("");
  $("#agent-provider").onchange=renderProviderFields;
  renderProvider(agent?.provider||"");
  $$("[data-close-modal]").forEach(button=>button.onclick=closeModal);
  $("#agent-form").onsubmit=async event=>{
    event.preventDefault();
    const data=Object.fromEntries(new FormData(event.target)),config=data.provider===agent?.provider?{...(agent.provider_config||{})}:{};
    try{
      if(data.provider==="a2a"){config.agent_card_url=data.agent_card_url;if(data.skill_id)config.skill_id=data.skill_id;if(data.token_env)config.token_env=data.token_env;if(data.output_schema)config.output_schema=JSON.parse(data.output_schema);}
      else if(data.provider==="acp"){config.command=data.command.trim().split(/\s+/);config.permission_policy="deny";}
      else {if(data.model)config.model=data.model;else delete config.model;if(data.provider==="claude")delete config.permission_mode;}
      await api("/api/agents",jsonOptions("POST",{id:data.id,name:data.name,runtime_id:data.runtime_id,provider:data.provider,instructions:data.instructions,provider_config:config}));
      closeModal();await refresh();toast(editing?"Agent 已更新":"Agent 已保存");
    }catch(error){toast(error.message,true);}
  };
}


async function refresh(){await loadData();render();checkHealth();}
async function refreshIfChanged() {
  if (state.selectedIssue || !$("#modal-backdrop").hidden) return;
  const before = JSON.stringify([state.issues, state.tasks, state.agents, state.squads, state.runtimes, state.capabilities, state.projects, state.projectCandidates]);
  await loadData();
  const after = JSON.stringify([state.issues, state.tasks, state.agents, state.squads, state.runtimes, state.capabilities, state.projects, state.projectCandidates]);
  if (before !== after) render();
  checkHealth();
}
$("#primary-action").onclick=()=>({issue:showCreateIssue,agent:showAgentForm,squad:showCreateSquad,runtime:probeConfiguredRuntime})[routeMeta[state.route][3]]();
$("#refresh-button").onclick=async()=>{try{await refresh();toast("数据已刷新");}catch(error){toast(error.message,true);}};
$("#drawer-backdrop").onclick=closeDrawer;$("#modal-backdrop").addEventListener("click",event=>{if(event.target===$("#modal-backdrop"))closeModal();});
window.addEventListener("hashchange",setRoute);window.addEventListener("keydown",event=>{if(event.key==="Escape"){closeModal();closeDrawer();}});
try{await loadData();setRoute();checkHealth();setInterval(()=>refreshIfChanged().catch(()=>{}),8000);}catch(error){$("#page").innerHTML=empty("工作台加载失败",esc(error.message));checkHealth();}
