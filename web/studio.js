"use strict";
const $ = id => document.getElementById(id);
const state = {book:null, tab:"layout", page:1, scene:"", part:0, region:"", tool:"select", dirty:false, timer:0, loading:false};
const names = {narration:"旁白",dialogue:"对白",bubble:"气泡或文字底板（整体修补）",prop:"道具文字",mark:"出版标记",note:"教学或其他内容"};
const stageNames = {import:"读取原页",analyze:"识别分镜",clean:"清理首帧",reclean:"重做清图",script:"编写制作稿",activities:"生成学习活动",voice:"合成配音",video:"生成逐幕动画",sample:"制作样片",all:"制作全书",export:"导出",proof:"导出校对版",reel:"导出动画样片",build:"自动制作整书",redraw:"重画分镜首帧",refresh_scene:"重新制作本幕"};
const node=(tag,cls="",text="")=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text)el.textContent=text;return el;};
function append(parent,...children){for(const c of children.flat())if(c)parent.append(c);return parent;}
function action(text,fn,cls=""){const b=node("button",cls,text);b.type="button";b.onclick=()=>Promise.resolve().then(fn).catch(error=>message(error.message));return b;}
function link(text,url,download=false){const a=node("a","link",text);a.href=url;if(download)a.download="";else{a.target="_blank";a.rel="noopener";}return a;}
function select(choices,value,fn,label){const s=node("select");if(label)s.setAttribute("aria-label",label);
 for(const [v,t] of choices){const o=node("option","",t);o.value=v;s.append(o);}s.value=String(value);s.onchange=()=>fn(s.value);return s;}
function field(label,value,fn,multi=false){const l=node("label","field");l.append(document.createTextNode(label));
 const e=node(multi?"textarea":"input");e.value=value??"";e.oninput=()=>{fn(e.value);dirty();};l.append(e);return l;}
function head(title,...controls){return append(node("div","panel-head"),node("h2","",title),append(node("div","row"),...controls));}
function dirty(){state.dirty=true;saveButton();}
function busy(){return state.book?.job?.state==="running"||state.loading;}
function saveButton(){$("save").disabled=!state.dirty||busy();$("save").textContent=state.dirty?"保存修改":"已保存";}
let toastTimer;
function message(msg){$("message").textContent=msg;$("message").hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("message").hidden=true,8500);}
async function req(path,options={}){const response=await fetch(path,options);let value;try{value=await response.json();}catch{throw Error("制作台连接中断，请重新打开本机网页。");}
 if(!response.ok)throw Error(value.error||"请求失败");return value;}
const send=(method,body)=>({method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
const api=()=>"/api/projects/"+state.book.id;
const media=(sub,file)=>"/media/"+state.book.id+"/"+sub+"/"+encodeURIComponent(file);
const pageImage=n=>media("pages","p"+String(n).padStart(4,"0")+".jpg");
const current=()=>state.book.scenes.find(s=>s.id===state.scene);
function projectUrl(id){return "/api/projects/"+id;}
async function loadBook(id){
 clearTimeout(state.timer);
 const b=await req(projectUrl(id));
 const switched=state.book?.id!==id;state.book=b;state.dirty=false;
 if(switched){state.page=1;state.scene="";state.part=0;state.region="";state.tool="select";state.tab="layout";}
 if(!current() && b.scenes.length){state.scene=b.scenes[0].id;state.part=0;state.page=current().parts[0].page;}
 state.page=Math.max(1,Math.min(b.pages.length||1,state.page));
 render();if(b.job.state==="running")poll(id);
}
async function persist(){
 if(!state.dirty)return;
 const b=state.book;
 state.book=await req(api(),send("PUT",{revision:b.revision,profile:b.profile,regions:b.regions,scenes:b.scenes,tts_model:b.tts_model,video_provider:b.video_provider||"heygen"}));
 state.dirty=false;render();
}
async function job(type,scope="",lang="",useMinimax=false,options={}){
 if(busy())return;
 state.loading=true;saveButton();
 try{
   await persist();
   if(type==="analyze" && state.book.scenes.length && !confirm("重新识别会替换当前场景编辑。已导出的版本仍可从原链接打开。继续吗？"))return;
   state.book.job=await req(api()+"/jobs",send("POST",{action:type,scene:scope,language:lang,revision:state.book.revision,use_minimax:useMinimax,...options}));status();poll(state.book.id);
 }
 finally{state.loading=false;render();}
}
async function requestRedraw(scene){
 const dialog=$("redraw-dialog"),form=$("redraw-form"),use=$("redraw-use-rsi");
 const errorBox=$("redraw-error");errorBox.hidden=true;
 const base=scene.art_prompt||scene.summary||scene.title;
 form.elements.prompt.value=base;form.elements.note.value="";use.hidden=true;
 $("redraw-scene").textContent=scene.title;dialog.showModal();
 $("cancel-redraw").onclick=()=>dialog.close();
 form.onsubmit=async e=>{
   e.preventDefault();if(busy())return;
   const prompt=form.elements.prompt.value.trim();
   if(!prompt){form.elements.prompt.focus();return;}
   const submit=form.querySelector('[type="submit"]');submit.disabled=true;
   try{
     state.sourceFor="";state.region="";state.tool="select";
     await job("redraw_scene",scene.id,"",false,{redraw_prompt:prompt,redraw_note:form.elements.note.value.trim()});
     dialog.close();
   }catch(error){errorBox.textContent=error.message;errorBox.hidden=false;}
   finally{submit.disabled=false;}
 };
 if(scene.redraw_edit){
   const result=await req(api()+"/rsi");
   const revision=result.events.map(e=>e.report?.redraw).find(r=>r?.id===scene.redraw_edit.id);
   if(dialog.open && current()?.id===scene.id && revision?.suggestion){
     use.onclick=()=>{form.elements.prompt.value=revision.suggestion;form.elements.prompt.focus();};
     use.hidden=false;
   }
 }
}
stageNames.redraw_scene="重画分镜";
function poll(id){clearTimeout(state.timer);state.timer=setTimeout(async()=>{
 if(state.book?.id!==id)return;
 try{const j=await req(projectUrl(id)+"/job");state.book.job=j;status();if(j.state==="running")poll(id);else{await loadBook(id);document.dispatchEvent(new Event("book-job-finished"));}}
 catch(e){message(e.message);poll(id);}
},1150);}
function status(){const j=state.book?.job;if(!j)return;const el=$("status");el.hidden=["idle","done"].includes(j.state);el.dataset.state=j.state;
 el.textContent=(stageNames[j.stage]||"制作")+" · "+j.message;}
function jobButton(text,type,scope="",cls="",lang=""){const b=action(text,()=>job(type,scope,lang),cls);b.disabled=busy();return b;}
function pickScene(s){state.scene=s.id;state.part=0;state.page=s.parts[0].page;state.region="";state.tool="select";render();}
function mark(edit=true){if(edit)dirty();render();}
function id(prefix){return prefix+"-"+crypto.randomUUID().slice(0,8);}
function selectScene(){return select(state.book.scenes.map((s,i)=>[s.id,String(i+1).padStart(2,"0")+" "+s.title]),state.scene,v=>pickScene(state.book.scenes.find(s=>s.id===v)),"当前场景");}
function render(){
 const b=state.book;if(!b)return;const title=b.profile.title||b.name;$("title").textContent=title;
 $("summary").textContent=b.pages.length+(b.creation?.kind==="text"?" 页插画":" 页原稿")+" · "+b.scenes.length+" 个场景";
 for(const bt of document.querySelectorAll("[data-tab]")){
 if(bt.dataset.tab===state.tab)bt.setAttribute("aria-current","step");else bt.removeAttribute("aria-current");}
 status();saveButton();const view=$("view");view.replaceChildren();
 if(!b.pages.length){append(view,append(node("div","editor"),node("p","",busy()?"正在准备画面。":"画面尚未载入。"),b.creation?jobButton("继续制作整本","build"):jobButton("重新读取原页","import")));return;}
 if(state.tab==="layout")layout(view);else if(state.tab==="profile")profile(view);else if(state.tab==="script")scripts(view);else output(view);
 if(busy())for(const el of view.querySelectorAll("input,textarea,.mutating"))el.disabled=true;
}
function addScene(){
 const s={id:id("scene"),title:"新场景",summary:"",parts:[{page:state.page,box:[0,0,1,1]}],sources:[]};
 const i=state.book.scenes.indexOf(current());state.book.scenes.splice(i<0?0:i+1,0,s);state.scene=s.id;state.part=0;state.tool="crop";mark();
}
function splitScene(){
 const s=current(), b=state.book;if(!s)return;
 const index=b.scenes.indexOf(s), next={id:id("scene"),title:s.title+"（续）",summary:s.summary,sources:[],parts:[]};
 if(s.parts.length>1){const cut=Math.max(1,state.part);next.parts=s.parts.splice(cut);}
 else{const p=s.parts[0],[x,y,r,d]=p.box,mid=(x+r)/2;p.box=[x,y,mid,d];next.parts=[{page:p.page,box:[mid,y,r,d]}];}
 const regionMap=new Map(b.regions.map(r=>[r.id,r]));
 next.sources=s.sources.filter(k=>{const v=regionMap.get(k);if(!v)return false;const x=(v.box[0]+v.box[2])/2, y=(v.box[1]+v.box[3])/2;
 return next.parts.some(p=>p.page===v.page&&p.box[0]<=x&&x<=p.box[2]&&p.box[1]<=y&&y<=p.box[3]);});
 s.sources=s.sources.filter(k=>!next.sources.includes(k));b.scenes.splice(index+1,0,next);dirty();pickScene(next);
}
function mergeScene(){
 const b=state.book,s=current(),i=b.scenes.indexOf(s),other=b.scenes[i+1];if(!s||!other)return;
 if(!confirm("把「"+other.title+"」拼入当前场景？"))return;
 if(b.profile.direction==="rtl")s.parts.unshift(...other.parts);else s.parts.push(...other.parts);
 s.sources=[...new Set([...s.sources,...other.sources])];s.summary+=" "+other.summary;b.scenes.splice(i+1,1);mark();
}
function move(delta){const a=state.book.scenes,i=a.indexOf(current()),j=i+delta;if(i<0||j<0||j>=a.length)return;[a[i],a[j]]=[a[j],a[i]];mark();}
function toolbarBtn(text,fn){const bt=action(text,fn,"mutating");bt.disabled=busy();return bt;}
function layout(view){
 const b=state.book,s=current();const desk=node("div","desk"),left=node("section","panel"),right=node("aside","panel");
 const pages=select(b.pages.map(p=>[p.n,"第 "+p.n+" 页"]),state.page,v=>{
   state.page=+v;state.region="";
   if(state.tool==="select"){
     const target=s?.parts.some(p=>p.page===state.page)?s:b.scenes.find(sc=>sc.parts.some(p=>p.page===state.page));
     if(target){state.scene=target.id;state.part=target.parts.findIndex(p=>p.page===state.page);}
   }
   render();
 },"原页");
 const onPage=s?.parts.some(p=>p.page===state.page);
 const previewKey=onPage && s.frame && s.frame===s.redrawn_frame ? b.id+"/"+s.id+"/"+s.frame : "";
 const preview=!!previewKey && state.sourceFor!==previewKey && !state.region && state.tool==="select";
 const toggle=previewKey?action(preview?"查看原页":"查看重画结果",()=>{
   state.sourceFor=preview?previewKey:"";state.tool="select";state.region="";render();
 }):null;
 append(left,head(preview?"分镜画面":"原页",toggle,pages));
 const bed=node("div","canvas-bed"),paper=node("div","paper"),img=node("img");
 img.src=preview?media("assets",s.frame):pageImage(state.page);
 img.alt=preview?"重画分镜 · "+s.title:"绘本原稿第 "+state.page+" 页";
 const over=node("div","overlay");over.hidden=preview;append(bed,append(paper,img,over));left.append(bed);
 if(onPage && !s.needs_illustration){
   const redraw=action("重画分镜",()=>requestRedraw(s),"primary redraw-frame");
   redraw.disabled=busy();redraw.title="用千问重画「"+s.title+"」的首帧";
   redraw.onpointerdown=e=>e.stopPropagation();
   paper.append(redraw);
 }
 function drawBox(bx,cls,caption=""){const el=node("div","box "+cls);el.style.left=100*bx[0]+"%";el.style.top=100*bx[1]+"%";el.style.width=100*(bx[2]-bx[0])+"%";el.style.height=100*(bx[3]-bx[1])+"%";
 if(caption)el.append(node("b","",caption));over.append(el);return el;}
 let activeBox;
 b.scenes.forEach((scene,i)=>scene.parts.forEach((p,pi)=>{if(p.page===state.page){
 const active=scene===s&&pi===state.part&&!state.region;const el=drawBox(p.box,active?"on corner":"",String(i+1));if(active)activeBox=el;}}));
 for(const r of b.regions.filter(r=>r.page===state.page&&(["words","region"].includes(state.tool)||r.id===state.region))){
 const active=r.id===state.region;const el=drawBox(r.box,"word "+(active?"on corner":""),active?names[r.kind]:"");if(active)activeBox=el;}
 function pos(e){const r=paper.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];}
 let drag=null;
 paper.onpointerdown=e=>{
 if(preview||busy()||e.button!==0)return;
 const [x,y]=pos(e),r=b.regions.find(a=>a.id===state.region),part=s?.parts[state.part];
 const target=r&&r.page===state.page?r:part?.page===state.page?part:null;
 let mode=state.tool,entry=target;
 if(mode==="words")return;
 if(mode==="select"){
 if(!entry||x<entry.box[0]-.025||x>entry.box[2]+.025||y<entry.box[1]-.025||y>entry.box[3]+.025)return;
 mode=Math.abs(x-entry.box[2])<.035&&Math.abs(y-entry.box[3])<.05?"resize":"move";}
 if(["crop","extend"].includes(mode)&&!s){message("请先添加或选择一个场景。");return;}
 drag={x,y,mode,entry,box:entry?.box?.slice(),now:null,ghost:drawBox([x,y,x,y],"on")};paper.setPointerCapture(e.pointerId);
 };
 paper.onpointermove=e=>{if(!drag)return;const [x,y]=pos(e);const d=drag,bb=d.box;let v;
 if(d.mode==="move"){const dx=Math.max(-bb[0],Math.min(1-bb[2],x-d.x)),dy=Math.max(-bb[1],Math.min(1-bb[3],y-d.y));v=[bb[0]+dx,bb[1]+dy,bb[2]+dx,bb[3]+dy];}
 else if(d.mode==="resize")v=[bb[0],bb[1],Math.max(bb[0]+.01,x),Math.max(bb[1]+.01,y)];
 else v=[Math.min(x,d.x),Math.min(y,d.y),Math.max(x,d.x),Math.max(y,d.y)];
 d.now=v.map(k=>Math.max(0,Math.min(1,+k.toFixed(5))));const g=d.ghost;g.style.left=100*v[0]+"%";g.style.top=100*v[1]+"%";g.style.width=100*(v[2]-v[0])+"%";g.style.height=100*(v[3]-v[1])+"%";};
 paper.onpointercancel=()=>{drag?.ghost.remove();drag=null;};
 paper.onpointerup=e=>{if(!drag)return;const d=drag;drag=null;d.ghost.remove();if(!d.now||d.now[2]-d.now[0]<.006||d.now[3]-d.now[1]<.006)return;
 if(d.mode==="region"){const r={id:id("text"),page:state.page,box:d.now,kind:"bubble",text:"",speaker:""};b.regions.push(r);state.region=r.id;if(s)s.sources.push(r.id);}
 else if(d.mode==="extend"){s.parts.push({page:state.page,box:d.now});state.part=s.parts.length-1;state.region="";}
 else if(d.mode==="crop"){const j=s.parts.findIndex(p=>p.page===state.page);if(j<0){message("请切到本幕原页，或使用“添加跨页画面”。");return;}state.part=s.parts[state.part]?.page===state.page?state.part:j;s.parts[state.part].box=d.now;state.region="";}
 else if(d.entry)d.entry.box=d.now;
 state.tool="select";mark();};
 const tools=node("div","toolbar");for(const [key,name] of [["select","移动 / 改大小"],["crop","框选本幕"],["extend","添加跨页画面"],["region","框选文字"],["words","显示文字框"]]){
 const bt=toolbarBtn(name,()=>{state.sourceFor=previewKey;state.tool=key;if(["crop","extend"].includes(key))state.region="";render();});bt.setAttribute("aria-pressed",!preview&&state.tool===key);tools.append(bt);}
 append(left,tools,node("p","hint",preview?"当前显示本幕重画后的首帧。":state.tool==="extend"?"切到相邻页，拖出要拼入本幕的原画范围。":state.tool==="select"?"拖动选区移动位置，拖右下角调整大小。":"在原页上按住并拖出矩形。"));
 const layout=b.layouts.find(p=>p.page===state.page);if(layout?.reason)left.append(node("p","hint",layout.reason));
 append(right,head("故事分镜",b.creation?.kind==="text"?null:jobButton(b.scenes.length?"重新识别":"识别全书分镜","analyze")));
 const rail=node("ol","rail");b.scenes.forEach((sc,i)=>{const bt=action("",()=>pickScene(sc));append(bt,node("strong","",sc.title),node("small","","p. "+sc.parts.map(p=>p.page).join(" + ")));
 const li=append(node("li",sc===s?"current":""),node("span","",String(i+1).padStart(2,"0")),bt);rail.append(li);});right.append(rail);
 append(right,append(node("div","toolbar"),toolbarBtn("添加场景",addScene),toolbarBtn("拆成两幕",splitScene),toolbarBtn("与下一幕拼接",mergeScene),toolbarBtn("上移",()=>move(-1)),toolbarBtn("下移",()=>move(1))));
 if(b.creation?.kind==="text"&&!b.visual_version){
  const redraw=action("重画分镜首帧",()=>job("redraw"),"mutating");redraw.disabled=busy();right.append(redraw);
 }
 if(s){append(right,field("场景名",s.title,v=>s.title=v),field("这一幕发生的事",s.summary,v=>s.summary=v,true));
 if(s.art_prompt)right.append(append(node("details"),node("summary","","本幕插画提示词"),node("p","scene-art-prompt",s.art_prompt)));
 const regenerate=action(s.video?"重新生成本幕视频":s.task?.state==="pending"?"继续生成本幕视频":"生成本幕视频",()=>{
   if(!s.video || confirm("为这一幕生成一段新的 MiniMax H3 动画？"))return job("refresh_scene",s.id,"",true);
 },"primary mutating");regenerate.disabled=busy();append(right,append(node("div","actions"),regenerate));
 const strip=node("div","strip");s.parts.forEach((p,i)=>strip.append(action((i+1)+" · 第 "+p.page+" 页",()=>{state.part=i;state.page=p.page;state.region="";state.tool="select";render();},i===state.part?"current":"")));
 append(right,strip);if(s.parts.length>1)append(right,append(node("div","row"),toolbarBtn("向左移一块",()=>{const i=state.part;if(!i)return;[s.parts[i-1],s.parts[i]]=[s.parts[i],s.parts[i-1]];state.part--;mark();}),toolbarBtn("移除所选画块",()=>{s.parts.splice(state.part,1);state.part=0;state.page=s.parts[0].page;mark();})));
 const remove=toolbarBtn("删除本幕",()=>{if(confirm("删除「"+s.title+"」？它的原句会保留在原页中。")){b.scenes.splice(b.scenes.indexOf(s),1);state.scene=b.scenes[0]?.id||"";state.part=0;mark();}});right.append(remove);}
 append(desk,left,right);view.append(desk);textRegions(view);if(b.coverage?.length)append(view,append(node("div","notice"),node("strong","","需要核对"),...b.coverage.map(c=>node("p","",c))));
}
function textRegions(view){
 const b=state.book,reg=b.regions.filter(r=>r.page===state.page),sec=node("section","editor");
 append(sec,head("原页文字",node("small","",reg.length+" 项")));const strip=node("div","strip");
 reg.forEach(r=>strip.append(action(r.text.slice(0,13)||names[r.kind],()=>{state.region=r.id;state.tool="select";render();},r.id===state.region?"current":"")));sec.append(strip);
 const r=reg.find(r=>r.id===state.region);if(r){const grid=node("div","grid-form"),parent=b.scenes.find(s=>s.sources.includes(r.id));
 const kind=select(Object.entries(names),r.kind,v=>{r.kind=v;if(!["narration","dialogue","bubble"].includes(v))for(const s of b.scenes)s.sources=s.sources.filter(k=>k!==r.id);mark();},"文字类别");
 const owner=select([["","待归属"],...b.scenes.map((s,i)=>[s.id,(i+1)+" "+s.title])],parent?.id||"",v=>{for(const s of b.scenes)s.sources=s.sources.filter(k=>k!==r.id);
 if(v){if(!["narration","dialogue","bubble"].includes(r.kind))r.kind="narration";b.scenes.find(s=>s.id===v).sources.push(r.id);}mark();},"对应场景");
 append(grid,field("原文",r.text,v=>r.text=v,true),field("说话人",r.speaker,v=>r.speaker=v),append(node("label","field"),"文字类别",kind),append(node("label","field"),"对应场景",owner));
 append(sec,grid); if(["narration","dialogue","bubble"].includes(r.kind))sec.append(toolbarBtn(r.ink_outlines?.length?"调整残笔轮廓":"精修印字边缘",()=>refineInk(r)));
 append(sec,toolbarBtn("删除这个标记",()=>{if(!confirm("删除这条文字标记？"))return;b.regions=b.regions.filter(t=>t!==r);for(const s of b.scenes)s.sources=s.sources.filter(k=>k!==r.id);state.region="";mark();}));}
 else if(!reg.length)sec.append(node("p","hint","识别分镜后，旁白、对白和道具文字会列在这里。也可以在上方框选文字。"));
 view.append(sec);
}
function profile(view){
 const b=state.book,p=b.profile,sec=node("section","editor");append(sec,head("这本书的资料"));const grid=node("div","grid-form");
 for(const [k,label,long] of [["title","书名"],["original_language","原书语种"],["age","适读年龄及依据"],["direction","阅读方向（ltr / rtl）"],["education","教育内容与原页依据",1],["style","原画风格",1],["glossary","中英专名和道具",1],["summary","全书情节",1]])grid.append(field(label,p[k],v=>p[k]=v,long));
 sec.append(grid);view.append(sec);
 const chars=node("section","editor");append(chars,head("角色与角度参考"));for(const c of (p.characters||[])){
 const row=node("div","role");append(row,field("角色",c.name,v=>c.name=v),field("固定外观",c.features,v=>c.features=v,true),node("small","",(c.refs||[]).map(r=>"第 "+r.page+" 页 "+r.view).join(" / ")));chars.append(row);}
 if(b.reference){const img=node("img","ref");img.src=media("assets",b.reference);img.alt="从原画提取的角色角度参考";chars.append(img);}
 view.append(chars);
}
function scripts(view){
 const b=state.book,s=current();if(!s){view.append(node("p","editor","请先在原页与分镜中建立场景。"));return;}
 const top=node("section","editor");append(top,head("清图对照",selectScene()));
 const pair=node("div","pair");for(const [k,title] of [["original","原画"],["frame","清理后的首帧"]]){const f=node("figure");const im=node("img");im.alt=title;
 if(s[k]){im.src=media("assets",s[k]);f.append(im);}else if(k==="original"){im.src=api()+"/original/"+s.id+"?rev="+b.revision;f.append(im);}else f.append(node("p","muted","本幕尚未清图"));
 append(pair,append(f,node("figcaption","",title)));}top.append(pair);
 append(top,append(node("div","actions"),jobButton(s.frame?"重新清图":"清理本幕首帧",s.frame?"reclean":"clean",s.id),jobButton("编写本幕双语制作稿","script",s.id,"primary")));
 if(s.script?.clean_warnings?.length)top.append(node("p","notice","清图复核 · "+s.script.clean_warnings.join(" / ")));
 if(s.clean_review?.length){
   const review=append(node("div","notice"),node("p","","请核对 · "+s.clean_review.join(" / ")));
   if(s.clean_approved)review.append(node("small","","本幕已人工核对"));
   else review.append(toolbarBtn("已看过原图对照，采用当前首帧",()=>{s.clean_approved=true;mark();}));
   top.append(review);
 }
 view.append(top);
 if(!s.script)return;
 const sc=s.script,sec=node("section","editor");append(sec,head("双语配音稿",node("small","",sc.lines.length+" 句")));
 for(const [i,line] of sc.lines.entries()){const card=node("div","line-edit");append(card,node("h3","",String(i+1).padStart(2,"0")+" · "+line.role));
 const grid=node("div","grid-form");for(const [l,name] of [["zh","中文"],["en","English"]]){
 append(grid,append(node("div"),field(name+"朗读正文",line[l],v=>line[l]=v,true),field(name+"朗读指令",line.direction[l],v=>line.direction[l]=v,true)));}
 card.append(grid);sec.append(card);}view.append(sec);
 const video=node("section","editor");append(video,head("首帧动作提示词"),field("本幕提示词",sc.video_prompt,v=>sc.video_prompt=v,true));video.querySelector("textarea").rows=5;view.append(video);
 const tracks=node("section","editor");append(tracks,head("本幕试听与预览"));
 for(const [l,name] of [["zh","中文"],["en","English"]].filter(([lang])=>!b.languages||b.languages.includes(lang))){const row=node("div","actions");append(row,node("strong","",name),jobButton("生成"+name+"配音","voice",s.id,"",l));if(s.audio?.[l]){const a=node("audio");a.controls=true;a.src=media("assets",s.audio[l].file);a.preload="none";row.append(a);}tracks.append(row);}
 if(s.video){const v=node("video");v.controls=true;v.preload="metadata";v.src=media("assets",s.video);tracks.append(v);}
 view.append(tracks);
}
function output(view){
 const b=state.book,s=current(),sec=node("section","editor");append(sec,head("生成素材",s?selectScene():null));
 if(!s){sec.append(node("p","hint","请先建立场景清单。"));view.append(sec);return;}
 append(sec,node("p","muted","各幕并行制作。完成后可打开互动播放器，或下载整本绘本包。"),
 append(node("div","actions"),jobButton("制作当前幕样片","sample",s.id,"primary"),jobButton("制作全书（"+b.scenes.length+" 幕）","all"),jobButton("导出当前校对版","proof"),jobButton("导出已完成动画","reel")));
 if(b.creation&&["error","interrupted"].includes(b.job.state))sec.append(jobButton("继续自动制作整本","build"));
 const provider=b.video_provider||"heygen";
 const engine=select([["heygen","HeyGen Video 1"],["minimax","MiniMax H3"]],provider,v=>{b.video_provider=v;mark();},"后续动画服务");
 sec.append(append(node("label","field"),"后续动画服务",engine));
 if(provider==="heygen" && s.frame && s.script){
   const exchange=node("section","editor");exchange.append(head("用 HeyGen 制作本幕"));
   const seconds=Math.min(30,Math.max(5,Math.ceil(s.planned_video_seconds||Math.max(s.audio?.zh?.duration||0,s.audio?.en?.duration||0))));
   exchange.append(node("p","hint",`本幕建议 ${seconds} 秒。可从本机网页用 HeyGen API 生成，也可在 HeyGen 选择图生视频，使用这张首帧和动作提示，再导入成片。`));
   const tools=append(node("div","actions"),link("下载本幕首帧",media("assets",s.frame),true),action("复制动作提示",async()=>{await navigator.clipboard.writeText(s.script.video_prompt);message("已复制本幕提示词");}),link("打开 HeyGen","https://app.heygen.com/"));
   if(!s.video){
     tools.append(jobButton("生成本幕动画","video",s.id));
     const upload=node("input");upload.type="file";upload.accept="video/mp4,.mp4";upload.hidden=true;
     upload.onchange=async()=>{const file=upload.files[0];if(!file)return;try{await persist();const revision=state.book.revision;state.loading=true;render();const q=new URLSearchParams({revision});await req(projectUrl(b.id)+"/clips/"+encodeURIComponent(s.id)+"?"+q,{method:"POST",headers:{"Content-Type":"video/mp4"},body:file});await loadBook(b.id);message("已接入本幕动画，可重新导出整书。");}catch(err){message(err.message);}finally{state.loading=false;render();}};
     tools.append(action("导入本幕 MP4",()=>upload.click(),"mutating"),upload);
   }
   exchange.append(tools);sec.append(exchange);
 }
 const fine=node("details");fine.append(node("summary","","按步骤制作"));const row=node("div","actions");
 for(const [k,t] of [["clean","全书清图"],["script","全书制作稿"],["activities","生成问答与小测"],["voice","全书双语配音"],["video","全书动画"],["export","打包全书"]])row.append(jobButton(t,k));append(fine,row);
 const model=select([["qwen-audio-3.1-tts-flash","Qwen-Audio 3.1 Flash · 支持表演指令"],["qwen3-tts-flash","Qwen3 TTS Flash · Cherry 系统音色"]],b.tts_model,v=>{b.tts_model=v;mark();},"千问语音模型");
 append(fine,append(node("label","field"),"语音模型",model));sec.append(fine);view.append(sec);
 if(s.task?.state==="submitting"&&!busy()){const r=node("section","editor");const input=node("input");input.placeholder="当前视频服务的任务号";
 append(r,head("恢复本幕视频任务"),input,action("关联任务号",async()=>{await req(api()+"/recover",send("POST",{scene:s.id,task:input.value}));await loadBook(b.id);}),action("确认未受理，允许重提",async()=>{
 if(confirm("已在对应视频服务的作品记录中确认提交未被受理？")){await req(api()+"/recover",send("POST",{scene:s.id,task:""}));await loadBook(b.id);}}));view.append(r);}
 const full=b.edition?.partial?null:b.edition;
 const editions=[full||(b.published&&!b.published.proof&&!b.published.partial?b.published:null),b.sample_edition||(b.edition?.partial?b.edition:null)].filter(Boolean);
 for(const ed of editions){const path="/media/"+b.id+"/editions/"+ed.id,card=node("section","editor edition");
 append(card,head(ed.partial?`已导出的动画样片 · ${ed.scenes.length} 幕`:"已导出的完整绘本"),append(node("div","actions"),link("打开交互播放器",path+"/index.html"),link("下载离线包",path+".zip",true),
 ...(b.languages||["zh","en"]).map(lang=>link((lang==="zh"?"中文":"英文")+"字幕 VTT",path+"/"+lang+".vtt",true))));view.append(card);}
 const proof=b.proof_edition||(b.published?.proof?b.published:null);
 if(proof){const path="/media/"+b.id+"/editions/"+proof.id;const review=node("section","editor edition");
 append(review,head("整书校对版"),node("p","hint",`${proof.animated} 幕动画，${proof.scenes.length-proof.animated} 幕显示校对静帧。`),append(node("div","actions"),link("打开整书校对版",path+"/index.html"),link("下载校对版 ZIP",path+".zip",true)));view.append(review);}
 const rejected=b.scenes.find(x=>!x.video && x.task?.state==="failed" && (x.task.provider||"minimax")===provider && x.task.message);
 if(rejected){const err=rejected.task.message;const detail=provider==="heygen" && /free videos for this month/i.test(err)?"HeyGen 返回本月免费视频额度已用完。已有动画已保存。":err;view.append(node("p","notice","最近一次未受理的镜头 · "+detail));}
 const panel=node("section","editor");append(panel,head("各幕进度"));const table=node("table","track-table");
 const headRow=node("tr");for(const t of ["场景","首帧","制作稿","中文","英文","动画"])headRow.append(node("th","",t));table.append(headRow);
 for(const [i,x] of b.scenes.entries()){const tr=node("tr");tr.append(append(node("td"),action((i+1)+". "+x.title,()=>{state.tab="script";pickScene(x);},"small")));
 for(const [index,v] of [x.frame,x.script,x.audio?.zh,x.audio?.en].entries())tr.append(node("td",v?"ready":"",v?"已完成":index>=2&&b.languages&&!b.languages.includes(index===2?"zh":"en")?"未选择":"待制作"));
 tr.append(node("td",x.video?"ready":"",x.video?(x.video_source?.provider==="heygen"?"HeyGen":"MiniMax H3"):"待制作"));table.append(tr);}
 panel.append(append(node("div","table-wrap"),table));view.append(panel);
}
$("save").onclick=()=>persist().catch(e=>message(e.message));
for(const b of document.querySelectorAll("[data-tab]"))b.onclick=()=>{state.tab=b.dataset.tab;render();};
window.addEventListener("beforeunload",e=>{if(state.dirty){e.preventDefault();e.returnValue="";}});
