"use strict";

const screens = ["home","text","pdf","library"];
const formTypes = ["text","pdf"];
const defaults = {zh:"longanhuan_v3.1",en:"Serena"};
const selectedBooks = {};
const formSettings = {};
let books = [], tones = [], openRating, libraryTimer;
const currentPage = () => screens.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home";
const editionUrl = (book, edition=book.edition?.id) => "/media/"+book.id+"/editions/"+edition+"/index.html";

const presets = {
  bees:["帮小蜜蜂找到回家的路","小狐狸、蜜蜂点点","春天的花园","通过查看花粉和果实，知道蜜蜂采蜜时也会给花朵传粉。"],
  moon:["今晚的月亮不一样","小兔子阿白、爷爷","山脚下的小院","观察弯月和圆月，知道月亮被太阳照亮的部分，从地球上看会呈现不同形状。"],
  share:["一盒彩笔怎么分","小熊、小松鼠、河狸","雨天的森林画室","练习轮流、协商和分享，理解每个伙伴都应当有参与的机会。"]
};

function buildChooser(type) {
  const holder = $(type+"-form").querySelector(".voice-chooser");
  const config = formSettings[type] ||= {language:"both",voices:{...defaults}};
  holder.replaceChildren();
  const options = [["zh","中文"],["en","English"],["both","中英双语"]];
  const toggle = append(node("div","language-control"),...options.map(([value,label])=>{
    const b = action(label,()=>{config.language=value;buildChooser(type);holder.querySelector('[aria-pressed="true"]').focus();});
    b.setAttribute("aria-pressed",String(config.language===value));
    return b;
  }));
  toggle.setAttribute("role","group");
  toggle.setAttribute("aria-label","语言");
  append(holder,node("h2","","语言与音色"),toggle);
  for(const lang of config.language==="both" ? ["zh","en"] : [config.language]){
    const label = lang==="zh" ? "普通话音色" : "英语音色";
    const choices = tones.map(voice=>[voice.id,voice.name+" · "+voice.style]);
    const control = select(choices,config.voices[lang],tone=>{config.voices[lang]=tone;},label);
    if(!tones.length)control.disabled=true;
    append(holder,append(node("label","field voice-label"),label,control));
  }
}

function cover(book) {
  const shell = node("div","book-cover");
  if (book.cover) {
    const image = node("img");
    image.alt = ""; image.loading = "lazy";
    image.src = "/media/"+book.id+"/"+(book.cover.endsWith(".jpg")?"pages":"assets")+"/"+encodeURIComponent(book.cover);
    image.addEventListener("error",()=>image.remove(),{once:true});
    shell.append(image);
  }
  return shell;
}

function volumeInfo(book){
  const language = book.languages?.length===1 ? (book.languages[0]==="en"?"英文":"中文") : "中英双语";
  const version = book.edition;
  if(!version) return language+" · "+(book.job.state==="running"?"制作中":"未导出");
  const label=version.partial?"动画样片":version.proof?"校对版 · "+version.animated+" 幕有动画":"交互绘本";
  return language+" · "+label;
}

function renderShelf(area,items,isLibrary){
  area.replaceChildren();
  if(!items.length){
    const box=node("div","shelf-empty");
    append(box,node("h2","",isLibrary?"还没有制作记录":"书架上还没有可播放的绘本"),
           node("p","",isLibrary?"提交故事设定或上传 PDF 后，绘本会出现在这里。":"第一次制作完成后，就能在这里再次阅读并留下评价。"));
    area.append(box);
    return;
  }
  for(const book of items){
    const item=append(node("article","shelf-card"),cover(book));
    const body=append(node("div","shelf-content"),node("h2","",book.title),node("p","book-meta",volumeInfo(book)));
    if(book.summary)body.append(node("p","book-excerpt",book.summary));
    const buttons=node("div","shelf-actions");
    if(book.edition)buttons.append(link("播放绘本",editionUrl(book)));
    if(isLibrary)buttons.append(action("分镜与评价",()=>openEditor(book.id)));
    else buttons.append(action("评价绘本",()=>showRating(book)));
    item.append(append(body,buttons));area.append(item);
  }
}

function showRating(book){
  openRating=book;
  const form=$("rating-form");form.reset();
  $("rating-book").textContent=book.title;
  $("rating-dialog").showModal();
}

function renderFeedback(){
  const container=$("book-feedback"),book=state.book;
  if(!book)return;
  container.replaceChildren();
  const feedback=Array.isArray(book.reviews)?book.reviews:[];
  const details=append(node("details","feedback-panel"),node("summary","",`家长评价（${feedback.length} 条）`));
  if(!feedback.length)details.append(node("p","hint","可在首页播放后留下评价。"));
  for(const entry of feedback){
    const caption=entry.rating+" / 5 分 · "+new Date(entry.created).toLocaleDateString("zh-CN");
    const row=append(node("div","review-entry"),node("strong","",caption));
    if(entry.notes)row.append(node("p","",entry.notes));
    details.append(row);
  }
  container.append(details);
  let questions=[];
  try{questions=JSON.parse(localStorage.getItem("picturebook-questions-"+book.id)||"[]");}catch{}
  if(Array.isArray(questions)&&questions.length){
    const review=append(node("details","feedback-panel"),node("summary","",`本机浏览器的阅读提问（${questions.length} 条）`));
    for(const entry of questions){
      if(!/^[a-f0-9]{24}$/.test(entry.edition)||typeof entry.question!=="string")continue;
      append(review,append(node("div","review-entry"),node("p","",entry.question),
        link("回看这一幕",editionUrl(book,entry.edition)+"?scene="+encodeURIComponent(entry.scene||""))));
    }
    container.append(review);
  }
}

async function openEditor(id){
  await persist();
  await loadBook(id);
  renderFeedback();
  $("workspace").hidden=false;
  $("workspace").scrollIntoView({behavior:"instant",block:"start"});
}

function renderPreview(type) {
  const pane=$(type+"-preview");
  const book=books.find(b=>b.id===selectedBooks[type]);
  const live=book?.job.state==="running";
  $(type+"-form").querySelector(".create-button").disabled=!!live || !!formSettings[type]?.uploading;
  pane.replaceChildren();
  if(!book){
    return append(pane,append(node("div","book-waiting"),node("h2","","绘本预览"),
      node("p","","制作完成后，在这里打开播放器。")));
  }
  if(live) {
    const progress = node("progress");
    progress.setAttribute("aria-label","整书制作中");
    const status = append(node("div","book-waiting"),node("h2","","正在制作整本绘本"),progress,node("p","",book.job.message));
    status.setAttribute("role","status");
    return append(pane,status);
  }
  if(book.edition) {
    const body=append(node("div","preview-copy"),node("h2","",book.title),node("p","book-meta",volumeInfo(book)),
      link(book.edition.proof?"打开整书校对版":"播放绘本",editionUrl(book)));
    append(pane,cover(book),body);
  }
  if(["error","interrupted"].includes(book.job.state)){
    const box=append(node("div","preview-copy"),node("h2","","制作暂停"),node("p","",book.job.message));
    append(box,append(node("div","shelf-actions"),
      action("继续制作整本",async()=>{await req(projectUrl(book.id)+"/jobs",send("POST",{action:"build"}));await refresh();},"primary"),
      action("查看分镜",async()=>{location.hash="library";await openEditor(book.id);})));
    pane.append(box);
  }else if(!book.edition){
    pane.append(append(node("div","book-waiting"),node("p","","可到绘本资料继续此项目。")));
  }
}

function showPage(){
  const active=currentPage();
  for(const type of screens)$(type+"-page").hidden=type!==active;
  for(const link of document.querySelectorAll("[data-page]")){
    if(link.dataset.page===active)link.setAttribute("aria-current","page");
    else link.removeAttribute("aria-current");
  }
  if(active==="home")renderShelf($("home-books"),books.filter(b=>b.edition),false);
  if(active==="library")renderShelf($("library-books"),books,true);
  if(formTypes.includes(active))renderPreview(active);
}

async function refresh(){
  clearTimeout(libraryTimer);
  books=await req("/api/projects");
  for(const type of formTypes){
    selectedBooks[type] ||= sessionStorage.getItem("picturebook-latest-"+type) ||
      books.find(b=>b.kind===type&&b.job.state==="running")?.id;
  }
  showPage();
  if(books.some(book=>book.job.state==="running")){
    libraryTimer=setTimeout(()=>refresh().catch(e=>message(e.message)),1700);
  }
}

async function submitBook(type){
  const form=$(type+"-form"),config=formSettings[type];
  if(config.uploading)return;
  const data=new FormData(form);
  config.uploading=true;renderPreview(type);
  const button=form.querySelector(".create-button");button.textContent="正在提交…";
  try{
    const {language,voices}=config;
    let result;
    if(type==="text"){
      const brief=Object.fromEntries(data.entries());
      result=await req("/api/stories",send("POST",{brief,language,voices}));
    }else{
      const file=$("file").files[0];
      if(!file || !/\.pdf$/i.test(file.name))throw Error("请选择一个 PDF 绘本。");
      const query=new URLSearchParams({name:file.name,build:"1",language,education:data.get("education"),...voices});
      result=await req("/api/projects?"+query,{method:"POST",headers:{"Content-Type":"application/pdf"},body:file});
    }
    selectedBooks[type]=result.id;
    sessionStorage.setItem("picturebook-latest-"+type,result.id);
    await refresh();
  }finally{
    config.uploading=false;button.textContent="生成整本绘本";renderPreview(type);
  }
}

for(const type of formTypes){
  buildChooser(type);
  $(type+"-form").onsubmit=e=>{e.preventDefault();submitBook(type).catch(error=>message(error.message));};
}
for(const button of document.querySelectorAll("[data-preset]"))button.onclick=()=>{
  const form=$("text-form");
  for(const [i,name] of ["theme","characters","setting","education"].entries())form.elements[name].value=presets[button.dataset.preset][i];
};
$("file").onchange=()=>{$("file-label").textContent=$("file").files[0]?.name||"选择或拖入 PDF 绘本";};
for(const event of ["dragover","dragleave","drop"])$("pdf-drop").addEventListener(event,e=>{
  e.preventDefault();
  $("pdf-drop").classList.toggle("dragging",event==="dragover");
  if(event==="drop"&&e.dataTransfer.files[0]){
    const files=new DataTransfer();files.items.add(e.dataTransfer.files[0]);$("file").files=files.files;$("file").onchange();
  }
});
const ratingChoices=$("rating-form").querySelector(".rating-choices");
for(let i=1;i<=5;i++){
  const input=node("input");input.type="radio";input.name="rating";input.required=true;input.value=i;
  append(ratingChoices,append(node("label"),input,node("span","",i+" 分")));
}
$("cancel-rating").onclick=()=>$("rating-dialog").close();
$("rating-form").onsubmit=async e=>{
  e.preventDefault();
  const form=e.currentTarget,data=new FormData(form),book=openRating,submit=form.querySelector('[type="submit"]');
  submit.disabled=true;
  try{
    const entry=await req(projectUrl(book.id)+"/reviews",send("POST",{rating:Number(data.get("rating")),notes:data.get("notes"),edition:book.edition.id}));
    if(state.book?.id===book.id){state.book.reviews=[entry,...(state.book.reviews||[])];renderFeedback();}
    $("rating-dialog").close();message("评价已保存到这本绘本的资料。");
  }catch(error){message(error.message);}finally{submit.disabled=false;}
};
$("close-editor").onclick=async()=>{try{await persist();$("workspace").hidden=true;}catch(error){message(error.message);}};
window.addEventListener("hashchange",showPage);
window.addEventListener("storage",renderFeedback);
document.addEventListener("book-job-finished",()=>{renderFeedback();refresh().catch(e=>message(e.message));});
showPage();
req("/api/health").then(info=>{
  tones=info.voices;
  for(const type of formTypes)buildChooser(type);
  const missing=["DeepSeek","千问","MiniMax"].filter(provider=>!info.models[provider]);
  $("connections").textContent=missing.length?"待配置 · "+missing.join("、"):"DeepSeek、千问、MiniMax 已配置";
}).catch(e=>message(e.message));
refresh().catch(e=>message(e.message));
