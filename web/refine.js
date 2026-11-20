"use strict";

async function refineInk(region) {
  const image = new Image();
  image.src = pageImage(region.page);
  await image.decode();
  const dialog = node("dialog", "ink-dialog");
  dialog.setAttribute("aria-label", "精修印字边缘");
  const canvas = node("canvas");
  canvas.width = 880;
  const [x, y, right, bottom] = region.box;
  const gapX = Math.max(.012, (right-x)*.08), gapY = Math.max(.012, (bottom-y)*.12);
  const left = Math.max(0,x-gapX), top = Math.max(0,y-gapY);
  const width = Math.min(1,right+gapX)-left, height = Math.min(1,bottom+gapY)-top;
  canvas.height = Math.max(40, Math.round(canvas.width*height*image.height/(width*image.width)));
  const ctx = canvas.getContext("2d");
  const outlines = structuredClone(region.ink_outlines || []);
  const points = [];
  const color = node("input"); color.type="color"; color.value=region.ink_color || "#e63831";
  const status = node("p", "hint"); status.setAttribute("role","status");
  let eyedropper = false;
  const nx = point => [(point[0]-left)/width*canvas.width, (point[1]-top)/height*canvas.height];
  function draw(list, closed) {
    if (!list.length) return;
    ctx.beginPath();
    for (const [i,p] of list.entries()) { const [px,py] = nx(p); if (i===0) ctx.moveTo(px,py); else ctx.lineTo(px,py); }
    if (closed) {ctx.closePath();ctx.fillStyle="rgba(0,122,180,.15)";ctx.fill();}
    ctx.strokeStyle="#056b93";ctx.lineWidth=2;ctx.stroke();
    if (!closed) for(const p of list) {const [px,py]=nx(p);ctx.fillStyle="#056b93";ctx.fillRect(px-2,py-2,5,5);}
  }
  function refresh() {
    ctx.drawImage(image,left*image.width,top*image.height,width*image.width,height*image.height,0,0,canvas.width,canvas.height);
    for (const outline of outlines) draw(outline,true);
    draw(points,false);
    status.textContent = eyedropper ? "点一下要去掉的笔画，取它的颜色。" : `已圈 ${outlines.length} 处，当前轮廓 ${points.length} 个点。`;
  }
  const source = node("canvas"); source.width=image.width; source.height=image.height;
  const pixels=source.getContext("2d", {willReadFrequently:true});pixels.drawImage(image,0,0);
  canvas.onpointerdown = event => {
    const r=canvas.getBoundingClientRect();
    const point=[left+width*(event.clientX-r.left)/r.width, top+height*(event.clientY-r.top)/r.height].map(v=>Math.max(0,Math.min(1,v)));
    if (eyedropper) {
      const rgb=pixels.getImageData(Math.min(image.width-1,Math.floor(point[0]*image.width)),Math.min(image.height-1,Math.floor(point[1]*image.height)),1,1).data;
      color.value="#"+Array.from(rgb.slice(0,3),v=>v.toString(16).padStart(2,"0")).join("");eyedropper=false;
    } else if (points.length<128) points.push(point);
    refresh();
  };
  function finishOutline() {
    if (points.length<3) {status.textContent="请至少点三个位置，围住要擦除的笔画。";return false;}
    if (outlines.length>=24) {status.textContent="一个文字框最多保存 24 个轮廓。";return false;}
    outlines.push(points.splice(0));refresh();return true;
  }
  append(dialog,head("精修印字边缘",action("关闭",()=>dialog.close())),node("p","hint","在原图上取印字颜色，再沿残笔外侧依次点出轮廓。保存后重新清理本幕。"),canvas,
    append(node("div","row"),append(node("label","row"),"印字颜色",color),action("从图中取色",()=>{eyedropper=true;refresh();}),
      action("撤回一点",()=>{points.pop();refresh();}),action("完成这一圈",finishOutline),action("撤回一圈",()=>{outlines.pop();refresh();})),status,
    append(node("div","actions"),action("采用轮廓",()=>{
      if(points.length&&!finishOutline())return;
      if(outlines.length){region.ink_outlines=outlines;region.ink_color=color.value;}else{delete region.ink_outlines;delete region.ink_color;}
      dialog.close();mark();
    },"primary")));
  document.body.append(dialog);dialog.addEventListener("close",()=>dialog.remove(),{once:true});dialog.showModal();refresh();
}