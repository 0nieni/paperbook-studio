"use strict";

const edition = window.EDITION;
const languages = edition.languages || ["zh","en"];
let locale = languages[0], book, media, shots, starts, total, sound;
const generated = new Set(edition.scenes.filter(s => s.video).map(s => s.id));
const asset = name => "assets/" + name;
const tr = (zh, en) => locale === "zh" ? zh : en;
const captionTracks = {};
function parseSubtitles(source) {
  const stamp = value => value.split(":").reduce((seconds, segment) => seconds * 60 + Number(segment), 0);
  const entities = {"&lt;": "<", "&gt;": ">", "&amp;": "&"};
  const cues = [];
  for (const block of source.trim().replace(/\r/g, "").split(/\n\s*\n/)) {
    const rows = block.split("\n");
    const timeRow = rows.findIndex(row => row.includes(" --> "));
    if (timeRow < 0) continue;
    const [start, end] = rows[timeRow].split(" --> ").map(stamp);
    if (!Number.isFinite(start) || !(end > start)) continue;
    const text = rows.slice(timeRow + 1).join("\n").replace(/&lt;|&gt;|&amp;/g, value => entities[value]);
    cues.push({start, end, text});
  }
  return cues;
}
for (const language of languages) {
  captionTracks[language] = parseSubtitles(edition.subtitles?.[language] || "");
  if (location.protocol === "http:" || location.protocol === "https:") {
    fetch(language + ".vtt").then(reply => {
      if (!reply.ok) throw Error(String(reply.status));
      return reply.text();
    }).then(source => {
      const cues = parseSubtitles(source);
      if (cues.length) { captionTracks[language] = cues; renderClock(); }
    }).catch(() => {});
  }
}
function localize() {
  const acts = edition.activities;
  sound = {};
  media = {};
  shots = edition.scenes.map(s => {
    const track = s.audio[locale], sc = s.script;
    media[s.id] = {...track, inset:[0,0,1,1]};
    const spots = sc.spots.map(h => {
      const [x,y,r,b] = h.box;
      sound[s.id+"-"+h.id] = track.spots[h.id];
      return [h.id,h.label[locale],(x+r)/2,(y+b)/2,r-x,b-y,h.explain[locale]];
    });
    return {id:s.id,title:sc.title[locale],page:s.pages.join(" / "),crop:[0,0,1,1],spots,raw:s};
  });
  const faq = acts.faq.map(f => {
    sound["faq-"+f.id] = edition.faq_audio[f.id]?.[locale];
    return {id:f.id,question:f.question[locale],reply:f.reply[locale],match:f.keywords[locale],evidence:f.evidence};
  });
  const ob=acts.observation;
  book = {shots,faq,observation:ob && {shot:ob.scene,answer:ob.spot,question:ob.question[locale],hint:ob.hint[locale]},
    quiz:acts.quiz.map(q=>({...q,question:q.question[locale],options:q.options.map(o=>o[locale]),
      hint:q.hint[locale],explain:q.explain[locale],match:q.keywords[locale]}))};
  starts=[0];
  for (const s of shots) starts.push(starts.at(-1)+media[s.id].duration);
  total=starts.at(-1);
}
localize();
const $ = (id) => document.getElementById(id);
const narration = $("narration");
const explanation = $("explanation");
const video = $("video");
const quizDialog = $("quiz");
const timeline = $("timeline");
const rail = $("scenes");
const state = {
  shot: 0, playing: false, started: false, captions: true,
  exploring: false, observation: false, observed: false, selected: "",
  resume: null, reviewing: false, seeking: false, videoReady: false,
  seek: 0, playRequest: 0
};
const quiz = {index: 0, answers: [], complete: false};
let activeRecognition;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function button(text, className, action) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function framePath(id) {
  return asset(edition.scenes.find(s=>s.id===id).frame);
}

function clock(seconds) {
  const value = Math.floor(Math.max(0, seconds) + 0.001);
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function renderClock() {
  const time = narration.currentTime || state.seek;
  const shot = shots[state.shot];
  const absolute = Math.min(total, starts[state.shot] + time);
  if (!state.seeking) timeline.value = String(absolute);
  $("timestamp").textContent = `${clock(absolute)} / ${clock(total)}`;
  timeline.setAttribute("aria-valuetext", tr(`${clock(absolute)}，共 ${clock(total)}`,`${clock(absolute)} of ${clock(total)}`));
  const caption = $("caption");
  const cueIndex = captionTracks[locale].findLastIndex(c => absolute >= c.start && absolute < c.end);
  let row = null;
  if (cueIndex >= 0) {
    row = {zh: captionTracks.zh?.[cueIndex]?.text || "", en: captionTracks.en?.[cueIndex]?.text || ""};
  } else if (!captionTracks[locale].length) {
    const cue = media[shot.id].cues.find(c => time >= c.start && time < c.end);
    row = cue && shot.raw.script.lines.find(t => t.id === cue.id);
  }
  const mode = $("cc").value;
  caption.textContent = !row ? "" : mode==="both" ? row.zh+"\n"+row.en : row[mode==="auto" ? locale : mode] || "";
  caption.hidden = !row || !state.started || mode==="off" || state.exploring;
}

function renderControls() {
  const label = state.playing ? tr("暂停","Pause") : tr("播放","Play");
  $("play").setAttribute("aria-label", label);
  $("play").title = label;
  $("play").firstElementChild.classList.toggle("paused", state.playing);
  $("welcome").hidden = state.started || state.exploring;
  $("prev").disabled = state.shot === 0;
  $("next").disabled = state.shot === shots.length - 1;
  $("find").setAttribute("aria-pressed", state.exploring);
  $("hotspots").hidden = !state.exploring;
  $("frame-note").hidden = !state.exploring;
  const showVideo = state.videoReady && state.started && !state.exploring;
  video.hidden = !showVideo;
  $("frame").hidden = showVideo;
  $("continue").hidden = !state.exploring || state.observation;
  $("ask-continue").hidden = !state.resume;
  renderClock();
}

function syncVideo() {
  if (!state.playing || !state.videoReady || state.exploring || !video.duration) return;
  const rate = video.duration / media[shots[state.shot].id].duration;
  video.playbackRate = Math.max(.25, Math.min(2, rate));
  const targetTime = Math.min(video.duration - .025, narration.currentTime * video.playbackRate);
  if (Math.abs(video.currentTime - targetTime) > .19) video.currentTime = targetTime;
  if (video.paused && !video.ended && targetTime < video.duration - .05) {
    video.play().catch(() => {});
  }
}

function stopExplanation() {
  explanation.pause();
}

function speak(id) {
  if (!sound[id]) return;
  explanation.src = asset(sound[id]);
  explanation.play().catch(() => {});
}

function pause() {
  state.playRequest++;
  state.playing = false;
  narration.pause();
  video.pause();
  renderControls();
}

function play() {
  stopExplanation();
  state.exploring = false;
  state.observation = false;
  state.started = true;
  if (!state.reviewing) state.resume = null;
  if (narration.ended && state.reviewing) {
    narration.currentTime = 0;
    if (state.videoReady) video.currentTime = 0;
  } else if (narration.ended) {
    if (state.shot === shots.length - 1) {
      state.observed = false;
      showShot(0);
    } else {
      showShot(state.shot + 1);
    }
  }
  const request = ++state.playRequest;
  state.playing = true;
  renderControls();
  narration.play().then(() => {
    if (request === state.playRequest) syncVideo();
  }).catch((error) => {
    if (request !== state.playRequest || error.name === "AbortError") return;
    pause();
    $("media-status").textContent = tr("旁白无法播放，请重新打开播放器","The narration could not play. Please reopen the player.");
  });
}

function showShot(index, time = 0, autoplay = false) {
  pause();
  stopExplanation();
  state.shot = index;
  state.seek = time;
  state.exploring = false;
  state.observation = false;
  state.selected = "";
  state.videoReady = false;
  const shot = shots[index];
  $("scene-number").textContent = String(index+1).padStart(2,"0");
  $("scene-title").textContent = shot.title;
  const maker = shot.raw.animation_model === "heygen-video-1" ? "HeyGen" : "MiniMax H3";
  $("media-status").textContent = shot.raw.video ? tr(`本幕 · ${maker} 动画`, `This scene · ${maker} animation`) : tr("本幕 · 校对静帧", "This scene · still illustration");
  $("scene-page").textContent = edition.source_kind==="text" ?
    tr(`插画 ${shot.page}`, `Illustration ${shot.page}`) : tr(`PDF 第 ${shot.page} 页`, `PDF p. ${shot.page}`);
  $("frame").src = framePath(shot.id);
  $("frame").alt = shot.title;
  narration.src = asset(shot.raw.audio[locale].file);
  video.removeAttribute("src");
  if (generated.has(shot.id)) video.src = asset(shot.raw.video);
  else video.load();
  rail.querySelectorAll(".scene-card").forEach((card, i) => card.setAttribute("aria-current", String(i === index)));
  const currentCard = rail.children[index];
  if (currentCard.offsetLeft - rail.offsetLeft < rail.scrollLeft
      || currentCard.offsetLeft - rail.offsetLeft + currentCard.offsetWidth > rail.scrollLeft + rail.clientWidth) {
    rail.scrollLeft = currentCard.offsetLeft - rail.offsetLeft;
  }
  renderObjects();
  renderSuggestions();
  renderControls();
  if (autoplay) play();
}

function seekTo(seconds, autoplay = state.playing) {
  const time = Math.max(0, Math.min(total - .02, seconds));
  const index = starts.findLastIndex((start) => start <= time);
  const offset = time - starts[index];
  state.started = true;
  if (index !== state.shot) {
    showShot(index, offset, autoplay);
    return;
  }
  state.seek = offset;
  if (narration.readyState) narration.currentTime = offset;
  if (state.videoReady) video.currentTime = Math.max(0, offset / media[shots[index].id].duration * video.duration - .02);
  renderControls();
}

function tab(name) {
  const asking = name === "ask";
  $("objects-panel").hidden = asking;
  $("ask-panel").hidden = !asking;
  $("objects-tab").setAttribute("aria-pressed", String(!asking));
  $("ask-tab").setAttribute("aria-pressed", String(asking));
}

function beginExplore(observation = false) {
  pause();
  stopExplanation();
  state.exploring = true;
  state.observation = observation;
  state.selected = "";
  tab("objects");
  renderObjects();
  renderControls();
}

function renderObjects() {
  const shot = shots[state.shot];
  const spots = $("hotspots");
  const list = $("object-list");
  spots.replaceChildren();
  list.replaceChildren();
  $("object-answer").hidden = true;
  $("objects-intro").textContent = state.observation ? tr("看看画面，找一找这一幕里的线索。","Look for a clue in this picture.") : tr("点一下物品，听听它的故事。","Choose an object to hear about it.");
  const [cropX, cropY, cropRight, cropBottom] = shot.crop;
  const [insetX, insetY, insetW, insetH] = media[shot.id].inset;
  for (const [id, label, x, y, width, height] of shot.spots) {
    const hotspot = button("", "hotspot", () => selectSpot(id));
    hotspot.dataset.object = id;
    hotspot.setAttribute("aria-label", label);
    hotspot.setAttribute("aria-pressed", "false");
    hotspot.title = label;
    hotspot.style.left = `${100 * (insetX + (x - cropX) / (cropRight - cropX) * insetW)}%`;
    hotspot.style.top = `${100 * (insetY + (y - cropY) / (cropBottom - cropY) * insetH)}%`;
    hotspot.style.width = `${100 * width / (cropRight - cropX) * insetW}%`;
    hotspot.style.height = `${100 * height / (cropBottom - cropY) * insetH}%`;
    spots.append(hotspot);
    if (!state.observation) {
      const chip = button(label, "chip", () => selectSpot(id));
      chip.dataset.object = id;
      chip.setAttribute("aria-pressed", "false");
      list.append(chip);
    }
  }
  if (state.observation) {
    const answer = $("object-answer");
    answer.replaceChildren(element("strong", "", book.observation.question), element("p", "observation-result", ""));
    answer.hidden = false;
    list.append(button(tr("跳过，继续故事","Skip and continue"), "outline", finishObservation));
  }
}

function selectSpot(id) {
  if (!state.exploring) beginExplore();
  const shot = shots[state.shot];
  const spot = shot.spots.find(([key]) => key === id);
  if (!spot) return;
  tab("objects");
  state.selected = id;
  document.querySelectorAll("[data-object]").forEach((node) => {
    node.setAttribute("aria-pressed", String(node.dataset.object === id));
  });
  const answer = $("object-answer");
  if (state.observation) {
    const feedback = answer.querySelector(".observation-result");
    const right = id === book.observation.answer;
    feedback.textContent = right ? tr("找到了！", "You found it! ") + spot[6] : book.observation.hint;
    if (right) {
      $("object-list").replaceChildren(button(tr("继续故事","Continue"), "outline", finishObservation));
      speak(`${shot.id}-${id}`);
    }
    return;
  }
  answer.replaceChildren(element("strong", "", spot[1]), element("p", "", spot[6]));
  answer.hidden = false;
  speak(`${shot.id}-${id}`);
}

function finishObservation() {
  state.observed = true;
  if (state.shot+1<shots.length) showShot(state.shot + 1, 0, true);
  else if (book.quiz.length) openQuiz();
  else showShot(0);
}

function reviewEvidence(id, returnToQuiz = false) {
  if (quizDialog.open) quizDialog.close();
  const resume = state.resume || {index: state.shot, time: narration.currentTime || state.seek};
  showShot(shots.findIndex((shot) => shot.id === id));
  beginExplore();
  state.reviewing = true;
  state.resume = resume;
  renderControls();
  if (returnToQuiz && !$("return-to-quiz")) {
    const back = button(tr("返回阅读小测","Back to quiz"), "main-button quiz-return", () => {
      back.remove();
      state.reviewing = false;
      state.resume = null;
      openQuiz();
    });
    back.id = "return-to-quiz";
    document.body.append(back);
    back.focus();
  }
  $("player").scrollIntoView({block: "nearest", behavior: "instant"});
}

function resumeFromReview() {
  state.reviewing = false;
  if (state.resume) {
    const {index, time} = state.resume;
    showShot(index, time, true);
  } else {
    play();
  }
}

function evidenceItems(ids, returnToQuiz = false) {
  const list = element("div", "evidence-list");
  for (const id of ids) {
    const shot = shots.find((item) => item.id === id);
    const link = $("evidence-template").content.firstElementChild.cloneNode(true);
    link.firstElementChild.src = framePath(id);
    link.lastElementChild.textContent = tr(`${shot.title} · 第 ${shot.page} 页`, `${shot.title} · p. ${shot.page}`);
    link.setAttribute("aria-label", tr(`回看 ${shot.title}，PDF 第 ${shot.page} 页`, `Revisit ${shot.title}, p. ${shot.page}`));
    link.addEventListener("click", () => reviewEvidence(id, returnToQuiz));
    list.append(link);
  }
  return list;
}

function renderSuggestions() {
  const target = $("suggestions");
  const current = shots[state.shot].id;
  const picks = [
    ...book.faq.filter((item) => item.evidence.includes(current)),
    ...book.faq.filter((item) => !item.evidence.includes(current)),
  ].slice(0, 3);
  target.replaceChildren(...picks.map((item) => button(item.question, "chip", () => ask(item.question, item))));
}

const studioEdition = location.protocol === "http:" && ["localhost", "127.0.0.1"].includes(location.hostname)
  && location.pathname.match(/^\/media\/([0-9a-f]{12})\/editions\/([0-9a-f]{24})\/index\.html$/);

function presetAnswer(question) {
  const normalize = text => text.toLowerCase().replace(/[\s\p{P}]+/gu, "");
  const words = normalize(question);
  if (!words) return null;
  const exact = book.faq.find(item => normalize(item.question) === words);
  if (exact) return exact;
  const matches = book.faq.map(item => {
    const terms = item.match.map(normalize).filter(token => token && words.includes(token));
    const rest = terms.sort((a, b) => b.length - a.length).reduce((s, token) => s.replaceAll(token, ""), words);
    return {item, score: words.length - rest.length};
  }).sort((a, b) => b.score - a.score);
  const best = matches[0];
  if (!best?.score) return null;
  // A shared word in a new question can refer to a different event.
  if (studioEdition && (best.score !== words.length || matches[1]?.score === best.score)) return null;
  return best.item;
}

function ask(question, known) {
  question = question.trim();
  if (!question) return;
  pause();
  stopExplanation();
  state.started = true;
  state.resume ||= {index: state.shot, time: narration.currentTime};
  tab("ask");
  const matched = known || presetAnswer(question);
  const scene = shots[state.shot].id;
  rememberQuestion(question, matched?.evidence || [scene]);
  const chat = $("chat");
  chat.append(element("p", "from-child", question));
  if (matched) {
    chat.append(element("p", "from-book", matched.reply), evidenceItems(matched.evidence));
    speak(`faq-${matched.id}`);
  } else if (studioEdition) {
    const response = element("p", "from-book");
    chat.append(response);
    askDeepSeek(question, scene, response);
  } else {
    chat.append(element("p", "from-book", tr("在本机制作台打开这本书，可以自由提问。离线阅读时可以点下面的问题，或输入随书关键词。","Open this book in the local studio to ask your own question. Offline, choose a question below or enter a book keyword.")));
  }
  chat.scrollTop = chat.scrollHeight;
  $("ask-input").value = "";
  renderControls();
}

async function askDeepSeek(question, scene, response) {
  const language = locale;
  response.textContent = tr("正在翻看这本书……", "Looking through the book…");
  try {
    const result = await fetch(`/api/projects/${studioEdition[1]}/ask`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({edition: studioEdition[2], language, scene, question})
    });
    const data = await result.json();
    if (!result.ok) throw Error(data.error || tr("制作台暂时没有应答。", "The studio is unavailable."));
    if (!response.isConnected) return;
    response.textContent = data.reply;
    if (data.evidence.length) {
      response.after(evidenceItems(data.evidence));
      rememberQuestion(question, data.evidence);
    }
  } catch (error) {
    if (!response.isConnected) return;
    response.textContent = tr("联网回答暂时不可用。", "Online answers are unavailable. ") + (error.message || "");
    response.append(" ", button(tr("重试", "Try again"), "chip", () => askDeepSeek(question, scene, response)));
  }
  $("chat").scrollTop = $("chat").scrollHeight;
}

function rememberQuestion(question, evidence) {
  if (!edition.book_id || location.protocol === "file:") return;
  const key = "picturebook-questions-" + edition.book_id;
  try {
    const entries = JSON.parse(localStorage.getItem(key) || "[]");
    const entry = {question:question.slice(0,300),scene:evidence[0],edition:location.pathname.split("/").at(-2)};
    localStorage.setItem(key, JSON.stringify([entry,...entries.filter(item=>item.question !== entry.question)].slice(0,25)));
  } catch {}
}

function listen(target, toggle, status) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (activeRecognition) {
    activeRecognition.stop();
    return;
  }
  if (!Recognition) {
    status.textContent = tr("这个浏览器暂时无法听写，可以直接输入。","You can type your question in this browser.");
    target.focus();
    return;
  }
  pause();
  stopExplanation();
  const recognition = new Recognition();
  recognition.lang = tr("zh-CN","en-US");
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  activeRecognition = recognition;
  toggle.setAttribute("aria-pressed", "true");
  status.textContent = tr("在听，请说话。","Listening. Go ahead.");
  recognition.onresult = (event) => {
    target.value = event.results[0][0].transcript;
    target.dispatchEvent(new Event("input", {bubbles: true}));
    status.textContent = tr("可以看看识别结果，确认后提交。","Check the words and submit when ready.");
    target.focus();
  };
  recognition.onerror = (event) => {
    status.textContent = event.error === "not-allowed" ? tr("请允许浏览器使用麦克风，或直接输入。","Allow microphone access, or type your answer.") : tr("没有收到清楚的语音，可以再试一次或直接输入。","Please try speaking again or type your answer.");
  };
  recognition.onend = () => {
    toggle.setAttribute("aria-pressed", "false");
    activeRecognition = null;
    if (status.textContent === tr("在听，请说话。","Listening. Go ahead.")) status.textContent = tr("听写已结束，可以再试一次。","Dictation has ended. You can try again.");
  };
  try {
    recognition.start();
  } catch {
    recognition.onend();
    status.textContent = tr("麦克风无法启动，可以直接输入。","The microphone could not start. You can type instead.");
  }
}

function currentAnswer() {
  return quiz.answers[quiz.index] ||= {selection: null, order: [], text: "", attempts: 0, done: false, correct: false, feedback: ""};
}

function freeFeedback() {
  return book.quiz[quiz.index].explain;
}

function checkQuiz() {
  const question = book.quiz[quiz.index];
  const answer = currentAnswer();
  if (answer.done) return;
  if (question.mode === "open") {
    if (!answer.text.trim()) return;
    answer.feedback = freeFeedback(answer.text);
    answer.done = true;
  } else {
    let correct = false;
    if (question.mode === "order" && answer.order.length === question.options.length) {
      correct = question.answer.every((n, i) => n === answer.order[i]);
    } else if (question.mode === "choice" && answer.selection !== null) {
      correct = answer.selection === question.answer;
    } else if (question.mode === "choice" && answer.text.trim()) {
      const text = answer.text.toLocaleLowerCase().replace(/[，。！？、\s]/g, "");
      const letter = text.match(/^(?:选|我选)?([ABCabc])(?:选项)?$/);
      correct = letter ? letter[1].toUpperCase().charCodeAt(0) - 65 === question.answer
        : question.match.some((group) => group.split("|").every((word) => text.includes(word.toLocaleLowerCase().replace(/\s/g,""))));
    } else {
      return;
    }
    answer.attempts++;
    answer.correct = correct;
    answer.done = correct || answer.attempts >= 2;
    answer.feedback = answer.done ? `${correct ? tr("答对啦！","That’s right! ") : tr("一起来看看。","Let’s take a look. ")}${question.explain}`
      : tr("再试一次。","Try again. ")+question.hint;
    if (!answer.done) {
      answer.selection = null;
      answer.order = [];
      answer.text = "";
    }
  }
  renderQuiz();
  $("quiz-feedback")?.focus();
}

function renderQuiz() {
  const body = $("quiz-body");
  const actions = $("quiz-actions");
  body.replaceChildren();
  actions.replaceChildren();
  if (quiz.complete) {
    const correct = quiz.answers.filter((answer) => answer.correct).length;
    body.append(element("h3", "quiz-question", tr(`答对 ${correct} / 5 道计分题`, `${correct} / 5 correct`)));
    body.append(element("p", "", tr("最后一道是说出自己的想法，不计分。","The last question is for sharing your own idea.")));
    actions.append(button(tr("再做一次","Try again"), "outline", () => {
      quiz.index = 0;
      quiz.answers = [];
      quiz.complete = false;
      renderQuiz();
    }), button(tr("回到故事","Back to story"), "main-button", () => quizDialog.close()));
    return;
  }
  const question = book.quiz[quiz.index];
  const answer = currentAnswer();
  body.append(element("div", "quiz-meta", tr(`第 ${quiz.index + 1} / ${book.quiz.length} 题 · ${question.type}`, `Question ${quiz.index+1} of ${book.quiz.length}`)),
    element("h3", "quiz-question", question.question));
  if (question.mode === "order") body.append(element("p", "", tr("按发生的先后顺序点选三张卡。","Choose all three cards in the order the events happened.")));
  if (question.options) {
    const group = element("div", question.options.every(Array.isArray) ? "options visual" : "options");
    for (const [index, value] of question.options.entries()) {
      const [text, ...images] = typeof value === "string" ? [value] : value;
      const option = button("", "option", () => {
        if (question.mode === "order") answer.order.push(index);
        else {
          answer.selection = index;
          answer.text = "";
        }
        renderQuiz();
        $("quiz-body").querySelectorAll(".option")[index]?.focus();
      });
      const order = answer.order.indexOf(index);
      option.setAttribute("aria-pressed", String(question.mode === "order" ? order >= 0 : answer.selection === index));
      option.disabled = answer.done || order >= 0;
      if (images.length) {
        const gallery = element("span", "choice-images");
        for (const id of images) {
          const img = element("img");
          img.src = framePath(id);
          img.alt = "";
          gallery.append(img);
        }
        option.append(gallery);
      }
      const prefix = question.mode === "order" ? (order >= 0 ? `${order + 1}. ` : "") : `${"ABC"[index]}. `;
      option.append(element("span", "", prefix + text));
      group.append(option);
    }
    body.append(group);
  }
  if (!answer.done && question.mode !== "order") {
    const form = element("div", "ask-form");
    const input = element("input");
    input.id = "quiz-response";
    input.type = "text";
    input.value = answer.text;
    input.placeholder = question.mode === "open" ? tr("我会说……因为……","I would say … because …") : tr("也可以说出你的答案","You can also give your answer in words.");
    input.setAttribute("aria-label", input.placeholder);
    const status = element("p", "voice-status");
    status.setAttribute("role", "status");
    const mic = button("", "mic", () => listen(input, mic, status));
    mic.setAttribute("aria-label", tr("语音答题","Speak your answer"));
    const icon = element("i", "mic-icon");
    icon.setAttribute("aria-hidden", "true");
    mic.append(icon);
    input.addEventListener("input", () => {
      answer.text = input.value;
      answer.selection = null;
      body.querySelectorAll(".option").forEach((node) => node.setAttribute("aria-pressed", "false"));
      $("quiz-submit").disabled = !input.value.trim();
    });
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") checkQuiz(); });
    form.append(input, mic);
    body.append(form, status);
  }
  if (answer.feedback) {
    const feedback = element("p", "quiz-feedback", answer.feedback);
    feedback.id = "quiz-feedback";
    feedback.tabIndex = -1;
    feedback.setAttribute("role", "status");
    body.append(feedback, evidenceItems(question.evidence, true));
  }
  if (answer.done) {
    actions.append(button(quiz.index === book.quiz.length - 1 ? tr("看结果","See results") : tr("下一题","Next question"), "main-button", () => {
      if (quiz.index === book.quiz.length - 1) quiz.complete = true;
      else quiz.index++;
      renderQuiz();
    }));
  } else {
    if (question.mode === "order" && answer.order.length) {
      actions.append(button(tr("重新排序","Start again"), "outline", () => { answer.order = []; renderQuiz(); }));
    }
    const submit = button(tr("提交答案","Check answer"), "main-button", checkQuiz);
    submit.id = "quiz-submit";
    submit.disabled = question.mode === "order" ? answer.order.length !== question.options.length
      : answer.selection === null && !answer.text.trim();
    actions.append(submit);
  }
}

function openQuiz() {
  if (quizDialog.open || !book.quiz.length) return;
  pause();
  stopExplanation();
  renderQuiz();
  quizDialog.showModal();
}

function translateControls() {
  const labels = [
    ["#quiz-top", "阅读小测", "Story quiz"], ["#quiz-title", "阅读小测", "Story quiz"],
    ["#start", "开始观看", "Start reading"], ["#frame-note", "首帧观察 · 点一下标记", "Tap a marker to explore"],
    ["#find", "找一找", "Explore"], ["#objects-tab", "找一找", "Explore"], ["#ask-tab", "问一问", "Ask"],
    ["#continue", "从原处继续播放", "Resume story"], ["#ask-continue", "从原处继续播放", "Resume story"],
    [".send", "发送", "Send"], [".companion-head h2", "故事伙伴", "Story companion"],
    [".companion-head p", "陪你一起看这本书", "Explore the book together"], [".scenes-heading h2", "故事分幕", "Scenes"]
  ];
  for (const [selector, zh, en] of labels) {
    const control = document.querySelector(selector);
    const word = Array.from(control.childNodes).find(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
    if (word) word.textContent = " " + tr(zh,en) + " ";
  }
  $("ask-input").placeholder = tr("问一问故事里的事", "Ask about the story");
  for (const [value, zh, en] of [["auto","跟随配音","Follow audio"],["both","中英双行","Both languages"],["off","关闭","Off"]]) {
    $("cc").querySelector(`[value="${value}"]`).textContent = tr(zh,en);
  }
  for (const [id,zh,en] of [["prev","上一幕","Previous scene"],["next","下一幕","Next scene"],["fullscreen","全屏","Full screen"],["ask-mic","语音提问","Ask by voice"]]) {
    $(id).setAttribute("aria-label", tr(zh,en)); $(id).title = tr(zh,en);
  }
}

function renderBook() {
translateControls();
rail.replaceChildren();
for (const [i, shot] of shots.entries()) {
  const card = button("", "scene-card", () => {
    state.started = true;
    state.resume = null;
    state.reviewing = false;
    showShot(i, 0, state.playing);
  });
  const figure = element("img");
  figure.src = framePath(shot.id);
  figure.alt = "";
  figure.loading = i > 8 ? "lazy" : "eager";
  const label = element("span");
  label.append(element("b", "", String(i+1).padStart(2,"0")), document.createTextNode(shot.title));
  card.append(figure, label);
  card.setAttribute("aria-label", tr(`第 ${i + 1} 幕 ${shot.title}`, `Scene ${i + 1}, ${shot.title}`));
  rail.append(card);
}

$("shot-count").textContent = tr("共 "+shots.length+" 幕",shots.length+" scenes");

$("media-status").classList.add("complete");
$("book-title").textContent = edition.title;
const bookLabel = languages.length===2 ? tr("双语交互绘本","Bilingual picture book") : tr("交互绘本","Interactive picture book");
document.title = edition.title+" · "+bookLabel;
$("edition-note").textContent = edition.proof ? tr(`校对版 · ${generated.size} 幕动画，${shots.length-generated.size} 幕静帧`, `Review edition · ${generated.size} animated, ${shots.length-generated.size} still`) : edition.partial ? tr("动画样片","Animation sample") : bookLabel;
$("quiz-top").hidden = !book.quiz.length;
$("ask-tab").hidden = !book.faq.length && !studioEdition;
$("voice-label").textContent=tr("配音","Audio");
$("cc-label").textContent=tr("字幕","Subtitles");
document.documentElement.lang = tr("zh-CN","en");
timeline.max = total.toFixed(3);
}
function mappedTime(sceneIndex,time,oldLanguage,newLanguage) {
  const pair=edition.scenes[sceneIndex].audio;
  const old=pair[oldLanguage].cues.findLast(c=>c.start<=time) || pair[oldLanguage].cues[0];
  const next=pair[newLanguage].cues.find(c=>c.id===old.id);
  const fraction=Math.max(0,Math.min(.999,(time-old.start)/Math.max(.001,old.end-old.start)));
  return next.start+fraction*(next.end-next.start);
}
$("language").addEventListener("change",()=>{
  const next=$("language").value, old=locale, i=state.shot;
  if (!languages.includes(next) || next===old) return;
  const time=mappedTime(i,narration.readyState ? narration.currentTime : state.seek,old,next);
  const wasPlaying=state.playing, exploring=state.exploring, observing=state.observation;
  const resume=state.resume && {...state.resume,time:mappedTime(state.resume.index,state.resume.time,old,next)};
  activeRecognition?.abort();
  locale=next; localize(); renderBook();
  $("chat").replaceChildren();
  showShot(i,time,wasPlaying);
  if (exploring) beginExplore(observing);
  state.resume=resume; renderControls();
});
$("language").value=locale;
if(languages.length===1){
  $("language").closest("label").hidden=true;
  for(const option of $("cc").options) if(!["auto","off",locale].includes(option.value)) option.hidden=true;
}
renderBook();
narration.addEventListener("loadedmetadata", () => { narration.currentTime = state.seek; });
narration.addEventListener("timeupdate", () => {
  if (!narration.currentTime && state.seek && narration.readyState < 1) return;
  state.seek = narration.currentTime;
  renderClock();
  syncVideo();
});
narration.addEventListener("ended", () => {
  pause();
  if (state.reviewing) beginExplore();
  else if (shots[state.shot].id === book.observation?.shot && !state.observed) beginExplore(true);
  else if (state.shot === shots.length - 1) { if (book.quiz.length) openQuiz(); else { state.started=false; showShot(0); } }
  else showShot(state.shot + 1, 0, true);
});
video.addEventListener("loadeddata", () => {
  state.videoReady = true;
  renderControls();
  syncVideo();
});
video.addEventListener("error", () => { state.videoReady = false; renderControls(); });
$("start").addEventListener("click", play);
$("play").addEventListener("click", () => {
  if (state.playing) beginExplore();
  else if (state.reviewing) play();
  else resumeFromReview();
});
$("find").addEventListener("click", () => {
  if (state.observation) return;
  if (state.exploring) {
    state.exploring = false;
    stopExplanation();
    renderControls();
  } else beginExplore();
});
$("objects-tab").addEventListener("click", () => tab("objects"));
$("ask-tab").addEventListener("click", () => tab("ask"));
$("continue").addEventListener("click", resumeFromReview);
$("ask-continue").addEventListener("click", resumeFromReview);
function adjacentShot(delta) {
  state.resume = null;
  state.reviewing = false;
  showShot(state.shot + delta, 0, state.playing);
}
$("prev").addEventListener("click", () => adjacentShot(-1));
$("next").addEventListener("click", () => adjacentShot(1));
$("cc").addEventListener("change",renderClock);
$("fullscreen").addEventListener("click", () => {
  const action = document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  action.catch(() => {});
});
$("rail-prev").addEventListener("click", () => rail.scrollBy({left: -rail.clientWidth * .7, behavior: "smooth"}));
$("rail-next").addEventListener("click", () => rail.scrollBy({left: rail.clientWidth * .7, behavior: "smooth"}));
timeline.addEventListener("input", () => {
  state.seeking = true;
  $("timestamp").textContent = `${clock(+timeline.value)} / ${clock(total)}`;
});
timeline.addEventListener("change", () => {
  const offset = +timeline.value;
  state.seeking = false;
  state.resume = null;
  state.reviewing = false;
  seekTo(offset);
});
$("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  ask($("ask-input").value);
});
$("ask-mic").addEventListener("click", () => listen($("ask-input"), $("ask-mic"), $("ask-voice-status")));
$("quiz-top").addEventListener("click", openQuiz);
$("quiz-close").addEventListener("click", () => quizDialog.close());
quizDialog.addEventListener("close", () => { if (activeRecognition) activeRecognition.abort(); });
document.addEventListener("keydown", (event) => {
  if (quizDialog.open || event.target.closest("input, textarea, button")) return;
  if (event.code === "Space") {
    event.preventDefault();
    $("play").click();
  }
  if (event.key === "ArrowLeft" && !$("prev").disabled) $("prev").click();
  if (event.key === "ArrowRight" && !$("next").disabled) $("next").click();
});
const reviewScene = new URLSearchParams(location.search).get("scene");
showShot(Math.max(0,shots.findIndex(s=>s.id===reviewScene)));
