"use strict";
document.documentElement.classList.add("js");
const $ = id => document.getElementById(id);
let timer = null, requestVersion = 0;
function notice(text, bad = false) { $("notice").textContent=text; $("notice").className=bad?"error":""; }
async function api(path, body) {
  const response = await fetch(path, body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || "请求未完成");
  return result;
}
async function safe(action) { try { await action(); } catch (error) { notice(error.message,true); } }
function context() {
  const d=document.body.dataset;
  return {project:d.project,view:d.view,episode:d.episode,item:d.item};
}
function schedule(running) {
  clearTimeout(timer);
  if (running) timer=setTimeout(()=>safe(refresh),2000);
}
async function refresh() {
  const version=++requestVersion;
  const query=new URLSearchParams(context());
  const page=await api("/api/view?"+query);
  if (version!==requestVersion) return;
  $("content").innerHTML=page.content;
  $("project").innerHTML=page.project_options;
  document.querySelector("nav").innerHTML=page.navigation;
  $("title").textContent=page.title;
  Object.assign(document.body.dataset,{project:page.project,view:page.view,episode:page.episode || "",item:String(page.item),running:String(page.background.running)});
  if (page.background.report?.error) notice(page.background.report.error,true);
  schedule(page.background.running);
}
function formBody(form, submitter) {
  const body=Object.create(null), data=new FormData(form);
  if (submitter?.name) data.set(submitter.name,submitter.value);
  for (const [name,raw] of data) {
    if (name.startsWith("_")) continue;
    let value=raw;
    const control=form.elements.namedItem(name);
    if (control?.hasAttribute?.("data-number")) {
      if (raw==="") {
        if (!control.hasAttribute("data-empty-null")) continue;
        value=null;
      } else {
        value=Number(raw);
        if (!Number.isFinite(value)) throw new Error("请输入有效的数字。");
      }
    }
    const parts=name.split(".");
    let target=body;
    for (const part of parts.slice(0,-1)) target=target[part] || (target[part]=Object.create(null));
    target[parts.at(-1)]=value;
  }
  if (["new","demo"].includes(body.action)) body.project=body.project.trim();
  if (body.action==="demo") delete body.novel;
  return body;
}
document.addEventListener("submit",event=>{
  const form=event.target;
  if (form.method.toLowerCase()!=="post") return;
  event.preventDefault();
  safe(async()=>{
    const body=formBody(form,event.submitter);
    const buttons=[...form.querySelectorAll("button[type=submit]")];
    buttons.forEach(b=>b.disabled=true);
    try {
      const result=await api("/api/command",body);
      if (["new","demo"].includes(body.action)) {
        location.assign("/?"+new URLSearchParams({project:result.project,view:"projects"}));
        return;
      }
      await refresh();
      notice(body.action==="pause"?"已请求暂停，当前调用结束后停止继续。":body.action==="settings"?"设置已保存，后续调用使用新设置。":"已保存。");
    } finally { buttons.forEach(b=>b.disabled=false); }
  });
});
document.addEventListener("change",event=>{
  const form=event.target.closest("form.selection-form");
  if (form) form.requestSubmit();
});
document.addEventListener("click",event=>{
  const button=event.target.closest("button[data-copy]");
  if (!button) return;
  safe(async()=>{await navigator.clipboard.writeText($(button.dataset.copy).textContent);notice("提示词已复制。");});
});
schedule(document.body.dataset.running==="true");
