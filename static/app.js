"use strict";
let csrf = "", state = null, editing = false, previousJob = "", polling = false, seenTuning = "", tuningTrigger = null, applyingTuning = false;
let tuningRequestError = "", tuningErrorRun = null;
let historyPreview = null, historyOptions = "";
let clientSnapshot = "";
const $ = id => document.getElementById(id);
const labels = {overview:"사용 안내",setup:"엔진 & 모델",tuning:"성능 튜닝",clients:"클라이언트",settings:"설정 & 캐시",activity:"상태 & 로그"};
const actions={"/api/install":"엔진 설치","/api/models/pull":"모델 다운로드","/api/models/verify":"모델 무결성 검증","/api/engine/start":"서버 시작","/api/engine/stop":"서버 중지","/api/cache/clear":"SSD 캐시 삭제","/api/tune":"실측 튜닝","/api/settings":"설정 저장","/api/integration":"클라이언트 실행기 변경","/api/connection":"기존 CLI의 Flint 연결 변경","/api/alias":"전용 명령 별칭 변경"};
function element(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; }
function showTab(name,scroll=0) {
  if(!labels[name])return;
  document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("active",e.id===name));
  document.querySelectorAll(".nav").forEach(e=>{e.classList.toggle("active",e.dataset.tab===name);e.setAttribute("aria-current",e.dataset.tab===name?"page":"false");});
  $("breadcrumb").textContent="워크스페이스 / "+labels[name];
  $("workspace-content").scrollTop=Number.isFinite(scroll)&&scroll>0?scroll:0;
}
// navigation.js (React + use-funnel) owns screen history when loaded; otherwise switch directly.
function tab(name) {
  if(document.documentElement.dataset.navigation==="funnel")window.dispatchEvent(new CustomEvent("flint:navigate",{detail:name}));
  else showTab(name);
}
window.addEventListener("flint:screen",e=>showTab(e.detail?.name,e.detail?.scroll));
let monitorVisual={state:"offline"};
function publishMonitor(detail){monitorVisual=detail;$("monitor").dataset.chartState=detail.state;window.dispatchEvent(new CustomEvent("flint:monitor",{detail}));}
window.addEventListener("flint:visuals-ready",()=>publishMonitor(monitorVisual));
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>tab(b.dataset.tab));
document.querySelectorAll("[data-goto]").forEach(b=>b.onclick=()=>tab(b.dataset.goto));
function confirmAction(text) {
  const d=$("confirm-dialog"); $("confirm-text").textContent=text; d.returnValue="";
  return new Promise(resolve=>{d.addEventListener("close",()=>resolve(d.returnValue==="confirm"),{once:true});d.showModal();});
}
function notify(text,error=false) { const e=$("notice");e.textContent=text;e.classList.toggle("alert",error);e.classList.remove("hidden"); }
async function api(path, body) {
  const options={method:body===undefined?"GET":"POST",headers:{"X-Flint-CSRF":csrf,...(body===undefined?{}:{"Content-Type":"application/json"})},body:body===undefined?undefined:JSON.stringify(body),credentials:"same-origin"};
  let response=await fetch(path,options);
  if(response.status===401&&body===undefined){
    const session=await fetch("/api/session",{credentials:"same-origin"});
    if(session.ok){csrf=(await session.json()).csrf;options.headers["X-Flint-CSRF"]=csrf;response=await fetch(path,options);}
  }
  const data=await response.json();if(!response.ok)throw Error(data.error||"요청 실패");return data;
}
async function action(path,body={}) {
  try { await api(path,body);$("connection").classList.add("hidden");notify("작업을 시작했습니다. 진행 상태와 활동 로그를 확인하세요.");await refresh();if(path==="/api/tune")openTuning(); }
  catch(e){notify(e.message,true);}
}
document.querySelectorAll("[data-action]").forEach(b=>b.onclick=async()=>{if(await confirmAction(b.dataset.confirm))action(b.dataset.action,b.dataset.source?{source:b.dataset.source}:{});});
$("start").onclick=()=>action("/api/engine/start");
$("stop").onclick=()=>action("/api/engine/stop");
$("cancel").onclick=()=>action("/api/cancel");
$("tune").onclick=async()=>{const p=state?.tuning_plan;if(!p?.allowed)return;if(await confirmAction(`worker ${p.worker_count}개 + 고정 chunk ${p.chunk_count}개 후보를 실제 모델 재시작으로 비교합니다. ${state.capabilities?.tune_reliability?"기준 응답을 3회 검증하고, 변경 후보가 있으면 측정 기준과 최종 조합을 2회 추가 재측정합니다. 최종 비교에서도 변경한 항목의 목표 속도가 3% 초과 개선되고 다른 속도가 3% 넘게 하락하지 않아야 제안합니다.":""} ${state.capabilities?.tune_recovery?"현재 workers로 시작하지 못하면 rec 기본 36/32/32를 측정 기준으로 시도합니다. 이 경우 복구와 성능 개선을 구분하며 추가 시험이 생길 수 있습니다. 멈춘 값보다 같은 축에서 더 큰 후보는 건너뜁니다.":""} 오래 걸릴 수 있고 취소할 수 있습니다. ${state.capabilities?.tune_apply?"측정 뒤 원래 설정과 실행 상태를 복원하고 제안은 확인 후 적용합니다.":"이 서버는 성공 시 자동 적용합니다 — 새 적용 흐름은 서버 재시작 후 사용됩니다."} 문맥과 캐시 사용 여부는 유지합니다. API 연결이 일시 중단되므로 다른 클라이언트 요청을 멈춘 뒤 진행하세요.`))action("/api/tune");};
$("baseline").onclick=async()=>{if(await confirmAction(`현재 설정 한 개만 실제 측정합니다. ${state.capabilities?.tune_reliability?"기준 응답 3회의 재현성이 확인되지 않으면 중단합니다.":""} 서버를 재시작하고 짧은/긴 입력 각각 3회 및 검증 요청을 수행한 뒤 원래 실행 상태를 복원합니다. 설정 채택은 하지 않습니다. 진행할까요?`))action("/api/tune",{mode:"baseline"});};
$("settings-form").addEventListener("input",()=>{editing=true;});
$("settings-form").onsubmit=e=>{
  e.preventDefault();const f=new FormData(e.target),v={};
  for(const key of Object.keys(state.settings))v[key]=key==="workers"?f.get(key).split(",").map(Number):key==="disk_cache"?f.has(key):Number(f.get(key));
  editing=false;action("/api/settings",v);
};
function fillSettings() {
  if(editing)return;
  const field=$("settings-form").elements.namedItem("workers"), current=state.settings.workers.join(",");
  const options=state.hardware?.worker_options||[];
  field.replaceChildren(...options.map(w=>{const option=element("option",w.join(" / ")+(JSON.stringify(w)===JSON.stringify(state.hardware.defaults.workers)?" — 칩 기본":""));option.value=w.join(",");return option;}));
  if(!options.some(w=>w.join(",")===current)){const option=element("option",current+" — 저장값 (검증 불가 또는 현재 하드웨어에서 무효)");option.value=current;field.append(option);}
  for(const [key,value] of Object.entries(state.settings)){const field=$("settings-form").elements.namedItem(key);if(key==="disk_cache")field.checked=value;else field.value=Array.isArray(value)?value.join(","):value;}
}
function renderHardware(s) {
  const h=s.hardware, plan=s.tuning_plan;
  if(!h||!plan){
    $("hardware-title").textContent="하드웨어 감지 · 재시작 필요";
    $("hardware-details").replaceChildren(element("p","하드웨어 감지를 적용하려면 웹 서버를 재시작하세요."));
    $("hardware-checks").replaceChildren();$("hardware-warning").textContent="";
    $("candidate-list").replaceChildren();$("candidate-description").textContent="재시작 전에는 새 하드웨어 기반 튜닝을 시작할 수 없습니다.";
  }else{
    const unknown=key=>"알 수 없음 · "+(h.reasons?.[key]||"감지하지 못했습니다.");
    $("hardware-title").textContent=(h.chip??unknown("chip"))+" · 감지 기반 후보";
    $("hardware-details").replaceChildren(
      element("p","GPU 코어 · "+(h.gpu_cores??unknown("gpu_cores"))),
      element("p","통합 메모리 · "+(h.memory_gib==null?unknown("memory_bytes"):`${h.memory_gib.toLocaleString()} GiB`)),
      element("p",`데이터 볼륨 여유 디스크 · ${(h.free_bytes/1024**3).toFixed(1)} GiB`));
    $("hardware-checks").replaceChildren(...h.checks.map(c=>element("p",`${c.passed===true?"통과":c.passed===false?"미충족":"알 수 없음"} · ${c.name}${c.passed===true?"":" · "+c.reason}`)));
    $("hardware-warning").textContent=[h.workers_status.reason,plan.reason].filter(Boolean).join(" · ");
    $("candidate-list").replaceChildren(...plan.workers.map(w=>element("code",w.join("/"))));
    $("candidate-description").textContent=`worker 단계 ${plan.worker_count}개 + chunk 단계 ${plan.chunk_count}개 후보 (고정 ${plan.chunks.join("/")}).${s.capabilities?.tune_reliability?" 변경 후보가 있으면 최종 비교 2회 추가.":""} 현재값 우선 측정, 후보 수에 따라 오래 걸릴 수 있으며 취소 가능합니다.${plan.allowed?"":" 튜닝 불가: "+plan.reason}`;
  }
  $("tuning-recovery").textContent=s.capabilities?.tune_recovery
    ?"전체 튜닝에서 현재 workers의 시작이 두 번 실패하면 rec 기본 36/32/32를 측정 기준으로 시도합니다. 원래 설정 대비 속도 향상으로 간주하지 않습니다. 준비 시간 초과 후보보다 같은 축에서 더 큰 값만 건너뛰며, 기록에 이유를 남깁니다. 현재값만 측정에서는 기본값으로 바꾸지 않습니다."
    :"기본값 측정 복구·멈춘 후보 건너뛰기를 사용하려면 웹 서버를 재시작하세요.";
  $("tuning-reliability").textContent=s.capabilities?.tune_reliability
    ?"기준 응답 3회의 해시가 모두 같아야 측정합니다. 사전 검사를 통과한 엔진의 준비 전 종료·준비 시간 초과만 한 번 재시도합니다. 변경 후보가 있으면 측정 기준과 최종 조합을 새로 시작해 생성·읽기 속도를 모두 재측정합니다. 측정 기준에서 변경한 항목의 목표 속도는 3% 초과 개선, 다른 속도는 3% 넘는 하락이 없어야 제안합니다."
    :"기준 응답 재현성 확인·제한된 시작 재시도·최종 조합 재측정을 사용하려면 웹 서버를 재시작하세요. 현재 서버에는 새 검증 흐름이 적용되지 않았습니다.";
  const previous=s.tuning.environment;
  $("tune-environment").textContent=previous?`측정 환경 · ${previous.chip??"칩 알 수 없음"} · GPU ${previous.gpu_cores??"알 수 없음"}코어 · ${previous.memory_gib??"알 수 없음"} GiB · macOS ${previous.macos??"알 수 없음"}${s.tuning_environment_status==="different"?" · 현재 환경과 다름: 재측정 권장":s.tuning_environment_status==="unknown"?" · 현재 환경을 완전히 읽지 못해 비교 불가":""}`:s.tuning.state?"측정 환경 기록 없음":"측정 후 환경을 기록합니다.";
}
function renderClients() {
  const details={claude:["Claude Code","claude는 기존 Claude · claude-flint는 로컬 Flint (별도 설정)","C"],codex:["Codex CLI","Responses API · 실행 인자로 로컬 provider 지정",">_"],opencode:["opencode","평소 opencode의 /models에서 다른 모델과 함께 선택","oc"],pi:["Pi","평소 pi의 /model에서 다른 모델과 함께 선택","π"]};
  const snapshot=JSON.stringify([state.integrations,state.job.state==="running",state.api_base,state.settings.context,state.settings.output_limit]);
  // Keep expanded help, keyboard focus, and text selection intact between status polls.
  if(snapshot===clientSnapshot)return;
  const root=$("client-list"), expanded=new Map([...root.children].map(card=>[card.id,card.querySelector(".client-details")?.open]));
  root.replaceChildren();
  for(const [name,value] of Object.entries(state.integrations)) {
    const [title,desc,icon]=details[name], card=element("article",undefined,"client-card");
    const shared=name==="pi"||name==="opencode", c=value.connection, alias=value.alias;
    const status=shared?(!c?"재시작 필요":c.error?"설정 확인 필요":c.needs_update?"갱신 필요":c.enabled?"Flint 등록됨":"미설정")
      :alias?.enabled?"전용 명령 준비됨":value.enabled?"실행기 준비됨":"미설정";
    const tone=shared?(!c||c.error||c.needs_update?"warning":c.enabled?"ready":""):value.enabled?"ready":"";
    const heading=element("div",undefined,"client-card-heading"), mark=element("span",icon,"client-icon");
    mark.setAttribute("aria-hidden","true");
    card.id=`client-${name}`;
    const titleElement=element("h2",title);titleElement.id=`client-title-${name}`;
    card.setAttribute("aria-labelledby",titleElement.id);
    heading.append(mark,titleElement,element("span",status,`client-status ${tone}`));
    const hint=shared?`${name} → ${name==="pi"?"/model":"/models"} → Flint Kit`
      :`${alias?.command||name+"-flint"} · 기존 ${name}와 별도 실행`;
    card.append(heading,element("p",hint,"client-hint"));
    const info=element("details",undefined,"client-details");
    info.open=expanded.get(card.id)||false;
    const summary=element("summary","사용법 · 경로 · 관리");
    summary.setAttribute("aria-label",`${title} 사용법 · 경로 · 관리`);
    info.append(summary,element("p",desc));
    if(shared){
      if(!c){
        card.append(element("p","기존 CLI 연결 기능을 사용하려면 웹 서버를 재시작하세요.","client-alert"));
      }else{
        info.append(element("p",c.enabled?"Flint 연결 기록 있음 · 기본 모델은 변경하지 않음":"기존 설정을 백업한 뒤 flint-local/flint 항목만 추가합니다."),
          element("pre",c.path||"설정 경로 확인 필요"));
        if(c.error)card.append(element("p",c.error,"client-alert"));
        if(c.needs_update)card.append(element("p","표시 이름·포트·문맥·출력 한도가 변경되었습니다. Flint 설정을 갱신하세요.","client-alert"));
        const buttons=element("div",undefined,"button-row");
        const connect=element("button",c.enabled?"Flint 설정 갱신":"기존 CLI에 Flint 추가","primary");
        connect.disabled=state.job.state==="running"||!c.can_connect||(c.enabled&&!c.needs_update);
        connect.onclick=async()=>{if(await confirmAction(`${c.path}\n이 파일을 백업하고 Flint provider만 ${c.enabled?"갱신":"추가"}할까요? 다른 모델·기본 모델·로그인·프로젝트 설정은 변경하지 않습니다. 주소 ${state.api_base}, 문맥 ${state.settings.context.toLocaleString()}, 출력 ${state.settings.output_limit.toLocaleString()}. 키는 파일 참조만 저장합니다.`))action("/api/connection",{name,enable:true});};
        buttons.append(connect);
        if(c.enabled){
          const remove=element("button","Flint 연결 해제","secondary");
          remove.disabled=state.job.state==="running"||!c.can_remove;
          remove.onclick=async()=>{if(await confirmAction(`${c.path}\nFlint 항목만 제거할까요? 이후 추가한 다른 모델과 설정은 보존합니다. Flint 항목 자체가 수정되었다면 자동 제거하지 않습니다.`))action("/api/connection",{name,enable:false});};
          buttons.append(remove);
        }
        card.append(buttons);
        info.append(element("p",name==="pi"?"pi → /model → Flint Kit (flint-local)":"opencode → /models → Flint Kit. 실행 중인 서비스가 설정을 유지하면 opencode reload로 다시 읽으세요."),
          element("p","Flint 선택 전 대시보드에서 서버를 시작하세요. 다른 모델은 기존 연결을 그대로 사용합니다. 다른 모델로 전환하면 그 서비스로 대화가 전송될 수 있습니다."));
        if(c.backup)info.append(element("p","최근 연결 백업"),element("pre",c.backup));
      }
      if(!value.enabled&&!alias?.enabled){card.append(info);root.append(card);continue;}
      info.append(element("h3","기존 격리 실행기 (선택 사항)"),element("p","아래 전용 명령은 이전 방식입니다. 위의 일반 CLI 연결과 별도로 유지·제거할 수 있습니다."));
    }
    if(name==="claude")info.append(element("p","모델 선택만으로 공식 서버와 로컬 서버를 오갈 수 없어 별도 명령을 사용합니다. 전용 명령 첫 실행 시 기존 온보딩 상태와 이미 신뢰한 폴더만 한 번 가져옵니다. 전용 설정의 기존 값은 보존하며 로그인·키·MCP·도구 권한은 복사하지 않습니다. 기존 Claude와 데스크톱 앱은 변경하지 않습니다."));
    info.append(element("pre",value.enabled?value.command:"전용 실행기 미생성"));
    const buttons=element("div",undefined,"button-row");
    const b=element("button",value.enabled?"실행기 복원 / 제거":"전용 실행기 생성",value.enabled?"secondary":"primary");b.disabled=state.job.state==="running"||value.alias?.enabled;
    b.onclick=async()=>{if(await confirmAction(value.enabled?`${title} 실행기를 복원할까요? 이후 수정된 파일은 보호하며 기존 전역 설정은 변경하지 않습니다.`:`${title} 전용 실행기를 Flint Kit 데이터 폴더에 생성할까요? 전역 설정이나 셸 PATH는 변경하지 않습니다.`))action("/api/integration",{name,enable:!value.enabled});};
    buttons.append(b);
    (shared?info:card).append(buttons);
    if(!alias){card.append(element("p","새 전용 명령 기능을 사용하려면 웹 서버를 재시작하세요.","client-alert"),info);root.append(card);continue;}
    info.append(element("p",`${alias.command} → Flint Kit 전용 · ${name} → 기존 CLI`));
    const a=element("button",alias.enabled?"전용 명령 제거":`${alias.command} 명령 설치`,"secondary");
    a.disabled=state.job.state==="running"||(!value.enabled&&!alias.enabled);
    a.onclick=async()=>{if(await confirmAction(alias.enabled?`${alias.path} 별칭을 제거할까요? 이후 변경된 파일은 보호합니다.`:`${alias.path}에 전용 실행기 링크를 만들까요? 같은 이름의 기존 파일은 덮어쓰지 않으며 셸 설정은 수정하지 않습니다.`))action("/api/alias",{name,enable:!alias.enabled});};
    buttons.append(a);
    if(alias.enabled){
      info.append(element("pre",alias.command),element("p","명령을 찾지 못하면 아래 전체 경로로 실행하세요. 터미널의 기존 동명 alias·함수는 자동 변경하지 않습니다."),element("pre",alias.path));
      info.append(element("p",alias.on_path?"웹 서버의 PATH에는 ~/.local/bin이 포함되어 있습니다. 터미널 PATH는 다를 수 있습니다.":"웹 서버의 PATH에 ~/.local/bin이 없습니다. 터미널에도 없다면 아래 명령으로 현재 터미널에서만 활성화하세요."));
      info.append(element("pre",'export PATH="$HOME/.local/bin:$PATH"'));
    }
    if(alias.enabled)info.append(element("p","실행기 제거 전 전용 명령부터 제거하세요."));
    card.append(info);root.append(card);
  }
  clientSnapshot=snapshot;
}
function renderHistory(s) {
  const select=$("tuning-history-select"), selected=select.value;
  const records=s.tuning_history||[];
  const signature=JSON.stringify(records);
  if(signature!==historyOptions){
    select.replaceChildren(...records.map(r=>{
      const e=r.environment, label=`${r.finished_at?new Date(r.finished_at*1000).toLocaleString("ko-KR"):"이전 결과"} · ${e?.chip??"환경 기록 없음"} / GPU ${e?.gpu_cores??"—"} / ${e?.memory_gib??"—"} GiB / macOS ${e?.macos??"—"} · 문맥 ${r.context?.toLocaleString()??"—"} · ${r.state}${r.current_only?" · 현재값":""}${r.applied?" · 적용됨":r.recovered?" · 복구 제안":""}`;
      const option=element("option",label);option.value=r.tuning_id;return option;
    }));
    select.value=records.some(r=>r.tuning_id===selected)?selected:records[0]?.tuning_id||"";
    historyOptions=signature;
  }
  select.disabled=!records.length;
  $("tuning-history-open").disabled=!records.length;
  $("tuning-history-note").textContent=s.capabilities?.tuning_history
    ?`${records.length}개 표시 (최근 100개까지) · 하드웨어·macOS·설정 문맥 조합별 기록입니다. 보관 기록은 읽기 전용이며 자동 적용하지 않습니다.`
    :"문맥별 튜닝 기록을 사용하려면 웹 서버를 재시작하세요.";
}
$("tuning-history-open").onclick=async()=>{
  const id=$("tuning-history-select").value;
  if(!id)return;
  try{
    historyPreview=await api("/api/tune/history/"+encodeURIComponent(id));
    if(!historyPreview)throw Error("튜닝 기록을 읽지 못했습니다.");
    renderTuning(state);openTuning(true);
  }catch(error){historyPreview=null;notify(error.message,true);}
};
function openTuning(keepHistory=false) {
  if(!keepHistory){historyPreview=null;renderTuning(state);}
  const d=$("tuning-dialog");
  if(d.open)return;
  tuningTrigger=document.activeElement;
  d.showModal();$("tuning-title").focus();
}
function closeTuning() {
  $("tuning-dialog").close();
  historyPreview=null;
  renderTuning(state);
  tuningTrigger?.focus();
}
$("tune-view").onclick=()=>openTuning();
$("tuning-close").onclick=closeTuning;
$("tuning-later").onclick=closeTuning;
$("tuning-dialog").addEventListener("cancel",e=>{e.preventDefault();closeTuning();});
$("tuning-cancel").onclick=()=>action("/api/cancel");
$("tuning-apply").onclick=async()=>{
  if(historyPreview||applyingTuning||!state?.capabilities?.tune_apply||!state.tuning.pending)return;
  tuningRequestError="";renderTuningError();
  applyingTuning=true;$("tuning-apply").disabled=true;
  try {await api("/api/tune/apply",{tuning_id:state.tuning.tuning_id});notify("제안을 적용하고 서버를 시작했습니다.");}
  catch(e){tuningRequestError=e.message;renderTuningError();notify(e.message,true);}
  finally{applyingTuning=false;await refresh();}
};
function renderTuningError() {
  $("tuning-request-error").textContent=historyPreview?"":tuningRequestError;
  $("tuning-request-error").classList.toggle("hidden",!!historyPreview||!tuningRequestError);
}
function median(values) {const v=values.filter(Number.isFinite).sort((a,b)=>a-b);return v.length?v[Math.floor(v.length/2)]:null;}
function speed(value) {return Number.isFinite(value)?value.toFixed(1)+" tok/s":"—";}
function tuningPhase(phase) {
  return {workers:"워커",fallback_baseline:"rec 기본값 측정 기준",chunk:"읽기 묶음",verify_baseline:"최종 비교 · 측정 기준",verify_candidate:"최종 비교 · 후보 조합",restore:"복원",complete:"종료"}[phase]||phase||"워커";
}
function trialStatus(r,t) {
  if(r.skipped)return "건너뜀 (미측정): "+r.skip_reason;
  if(r.error)return "실패 / 제외: "+r.error;
  if(r.score==null)return "측정 불충분 / 제외";
  if(r.phase==="verify_candidate")return t.verification?.state==="passed"?"최종 검증 통과":t.verification?.state==="rejected"?"최종 채택 보류":"최종 판정 대기";
  return r.phase==="verify_baseline"?"비교 기준 재측정 완료":"측정 완료";
}
function retryNote(r) {return r.start_errors?.length?` · 엔진 시작 ${r.start_attempts}회 · 이전 실패: ${r.start_errors.join(" / ")}`:"";}
function finite(value){return Number.isFinite(value)&&value>=0;}
function fixed(value,places=1){return finite(value)?value.toLocaleString("ko-KR",{maximumFractionDigits:places}):"—";}
function prefillSpeed(row){return row&&finite(row.prefilled_tokens)&&finite(row.prefill_seconds)&&row.prefill_seconds>0?row.prefilled_tokens/row.prefill_seconds:null;}
const CHART_SLOTS=12, chartSignatures={};
const CHART_INTERVALS=["5","60","300"], CHART_STORAGE_KEY="flint.chartSeconds";
let chartSeconds=300;
function supportsChartIntervals(s) {
  const intervals=s?.capabilities?.chart_interval_seconds;
  return Array.isArray(intervals)&&CHART_INTERVALS.every(value=>intervals.includes(Number(value)));
}
function saveChartSeconds(value) {
  try {window.localStorage.setItem(CHART_STORAGE_KEY,value);}
  catch(e){
    if(e.name!=="SecurityError"&&e.name!=="QuotaExceededError")throw e;
    notify("차트 시간 단위는 적용하지만 브라우저에 저장하지 못했습니다. 새로고침하면 선택이 초기화될 수 있습니다.",true);
  }
}
function readChartSeconds() {
  try {
    const saved=window.localStorage.getItem(CHART_STORAGE_KEY);
    if(saved===null)return 300;
    if(CHART_INTERVALS.includes(saved))return Number(saved);
    if(saved==="900"){
      notify("15분 단위가 제거되어 기본 5분 단위로 변경했습니다.");
      saveChartSeconds("300");
      return 300;
    }
    notify("저장된 차트 시간 단위가 올바르지 않아 기본 5분 단위를 사용합니다.",true);
  }catch(e){
    if(e.name!=="SecurityError"&&e.name!=="QuotaExceededError")throw e;
    notify("브라우저에서 차트 시간 단위를 읽을 수 없어 기본 5분 단위를 사용합니다.",true);
  }
  return 300;
}
function chartStatus(message,busy=false) {
  if($("chart-interval-status").textContent!==message)$("chart-interval-status").textContent=message;
  if($("chart-interval").title!==message)$("chart-interval").title=message;
  for(const name of ["decode","prefill","ttft"])$(`${name}-chart`).setAttribute("aria-busy",String(busy));
}
$("chart-interval").onchange=async()=>{
  const value=$("chart-interval").value;
  if(!CHART_INTERVALS.includes(value)){notify("차트 시간 단위는 5초, 1분, 5분 중에서 선택하세요.",true);return;}
  chartSeconds=Number(value);
  saveChartSeconds(value);
  chartStatus("시간대 집계 중입니다. 완료 전 차트는 이전 시간 단위로 표시됩니다.",true);
  await refresh();
};
const chartMeaning={
  decode:"구간 값은 생성 시간으로 가중한 평균이며, 한 토큰만 생성해 속도를 잴 수 없는 응답은 제외합니다.",
  prefill:"구간 값은 새로 처리한 입력 토큰 ÷ 처리 시간이며, 캐시로 처리할 입력이 없던 요청은 제외합니다.",
  ttft:"구간 값은 첫 응답 대기의 중앙값이며 낮을수록 빠릅니다.",
};
function durationText(seconds){return seconds%3600===0?`${seconds/3600}시간`:seconds%60===0?`${seconds/60}분`:`${seconds}초`;}
function clock(seconds,withSeconds=false){return finite(seconds)?new Date(seconds*1000).toLocaleTimeString("ko-KR",{hour:"2-digit",minute:"2-digit",...(withSeconds?{second:"2-digit"}:{}),hourCycle:"h23"}):"";}
function setAxis(name,parts) {
  const caption=$(`${name}-chart-label`), key=parts.join("|");
  if(caption.dataset.axis===key)return;
  caption.dataset.axis=key;caption.replaceChildren(...parts.map(text=>element("span",text)));
}
// mode: "series" (clock-aligned slots from the server), "stopped" (no engine run) or "restart" (server predates the series).
function renderRequestChart(name,label,mode,slots,seconds) {
  const chart=$(`${name}-chart`), unit=name==="ttft"?"s":"tok/s", places=name==="ttft"?2:1, series=mode==="series";
  const values=slots.map(b=>series&&finite(b?.[name])?b[name]:null);
  const counts=slots.map(b=>series&&finite(b?.requests)?b.requests:0);
  const maximum=Math.max(0,...values.filter(v=>v!==null));
  const heights=values.map(v=>v===null?null:maximum>0?v/maximum:0);
  const requests=counts.reduce((sum,n)=>sum+n,0), measured=values.filter(v=>v!==null).length;
  const reading=i=>values[i]===null?"측정값 없음":`${fixed(values[i],places)} ${unit}`;
  const titles=slots.map((b,i)=>series?[finite(b?.start)?`${clock(b.start,seconds<60)}–${clock(b.start+seconds,seconds<60)}`:"",
    counts[i]?`요청 ${counts[i]}개 · ${reading(i)}`:"요청 없음"].filter(Boolean).join(" · "):"");
  const span=durationText(seconds*CHART_SLOTS), step=durationText(seconds);
  const description=mode==="restart"?`${label} · 서버를 재시작하면 시간대별 차트를 볼 수 있습니다`
    :!series?`${label} · 엔진 중지됨`
    :!requests?`${label} · 최근 ${span} 동안 완료한 요청 없음`
    :!measured?`${label} · 최근 ${span} 동안 완료한 요청 ${requests}개에 측정값이 없습니다. ${chartMeaning[name]}`
    :`${label} · 최근 ${span}, ${step} 단위 · ${slots.flatMap((b,i)=>counts[i]?[`${clock(b?.start,seconds<60)} ${reading(i)} (요청 ${counts[i]}개)`]:[]).join(", ")}. 막대는 0부터 이 차트의 최댓값 기준이며 마지막 구간은 진행 중입니다. ${chartMeaning[name]}`;
  const empty=!series?(mode==="restart"?"재시작 필요":"중지됨"):!requests?"요청 없음":measured?"":"측정값 없음";
  setAxis(name,series?[`${span} 전`,`${step} 단위`,"지금"]:[mode==="restart"?"서버 재시작 필요":"엔진 중지됨"]);
  // Polling must not replace the slots: a changed node would drop a tooltip the pointer is resting on.
  const signature=JSON.stringify([empty,description,titles,heights]);
  if(chartSignatures[name]!==signature){
    chartSignatures[name]=signature;
    const host=$(`${name}-bars`);
    if(host.children.length!==CHART_SLOTS)host.replaceChildren(...Array.from({length:CHART_SLOTS},()=>{
      const slot=element("span",undefined,"chart-slot");slot.append(element("span",undefined,"chart-bar"));return slot;
    }));
    [...host.children].forEach((slot,i)=>{slot.title=titles[i];slot.children[0].style.height=`${(heights[i]??0)*100}%`;});
    chart.setAttribute("aria-label",description);chart.title=series?`최근 ${span} · ${step} 단위`:description;
    chart.dataset.empty=empty;
  }
  return heights;
}
function renderMonitor(s) {
  const selectable=supportsChartIntervals(s);
  $("chart-interval").disabled=!selectable;
  $("chart-interval").value=String(selectable?chartSeconds:300);
  chartStatus(selectable?"생성·입력·첫 응답 대기 차트에 함께 적용됩니다. 메모리는 현재값입니다."
    :"시간 단위를 변경하려면 웹 서버를 재시작하세요. 현재 차트는 5분 단위입니다.");
  const rows=(s.metrics||[]).filter(r=>["complete","cancelled","error","cancelled_or_failed"].includes(r.status));
  const last=[...rows].reverse().find(r=>r.status==="complete");
  $("prefill").replaceChildren(document.createTextNode(fixed(prefillSpeed(last))),element("small"," tok/s"));
  const currentRun=s.engine.running&&finite(last?.time)&&finite(s.monitor?.engine_started_at)&&last.time>=s.monitor.engine_started_at;
  $("request-label").textContent=last?`${currentRun?"이번 실행":"이전 실행 포함"} · 최근 완료 출력 ${fixed(last.generated_tokens,0)}토큰`:"아직 완료된 요청 없음";
  const memory=s.monitor?.memory;
  $("memory").replaceChildren(document.createTextNode(finite(memory?.rss_bytes)?(memory.rss_bytes/1024**3).toFixed(1):"—"),element("small"," GiB"));
  $("memory-label").textContent=!memory?"서버 재시작 필요":memory.state==="stopped"?"중지됨":memory.state==="unavailable"?"메모리 확인 불가":"RSS 합계 · 5초 간격";
  $("memory-detail").textContent=!memory?"메모리 모니터링을 사용하려면 웹 서버를 재시작하세요.":memory.state==="measured"
    ?`현재 RSS 합계 ${(memory.rss_bytes/1024**3).toFixed(2)} GiB · 소유 프로세스 ${memory.process_count}개 · ${new Date(memory.sampled_at*1000).toLocaleTimeString("ko-KR")} 측정`
    :memory.error||"엔진이 중지되어 현재 메모리 사용량은 표시하지 않습니다.";
  $("memory-peaks").textContent=`이번 실행의 관측 최대 RSS ${finite(memory?.sampled_peak_bytes)?(memory.sampled_peak_bytes/1024**3).toFixed(2):"—"} GiB · 최근 완료 요청의 MLX 피크 ${finite(last?.peak_mlx_bytes)?(last.peak_mlx_bytes/1024**3).toFixed(2):"—"} GiB. 관측 최대치는 5초 간격 샘플의 최댓값이며 순간 피크를 놓칠 수 있습니다. 요청 피크는 이전 실행이나 튜닝 기록일 수 있습니다.`;
  const errors=s.monitor?.errors||[];
  const exitNotice=Number.isInteger(s.engine.exit_code)?`엔진이 종료되었습니다 (코드 ${s.engine.exit_code}).`:"";
  const problem=s.job.state==="error"?s.job.message:exitNotice||s.monitor?.error_read_failure||memory?.error||
    (errors.length?errors.map(r=>r.category).join(" · "):rows.at(-1)?.status==="error"?"최근 요청이 실패했습니다. 상태 상세에서 확인하세요.":rows.at(-1)?.status==="cancelled_or_failed"?"최근 요청 미완료 · 취소 또는 실패 기록을 확인하세요.":"");
  $("monitor-error").textContent=problem;
  $("monitor-error").title=problem;
  $("monitor-error").classList.toggle("hidden",!problem);
  $("engine-errors-note").textContent=!s.monitor?"오류 요약을 사용하려면 웹 서버를 재시작하세요.":s.monitor.error_read_failure||
    `${exitNotice} ${errors.length?"최근 로그에 아래 오류 흔적이 있습니다.":"조회 범위에서 분류 가능한 오류 흔적이 없습니다. 모든 오류가 없다는 뜻은 아닙니다."}`.trim();
  $("engine-errors").replaceChildren(...errors.map(r=>element("li",`${r.category} · 로그 ${r.lines}줄`)));
  $("request-history").replaceChildren(...[...rows].reverse().map((r,index)=>{
    const tr=element("tr"), label={complete:"완료",cancelled:"취소",error:"실패",cancelled_or_failed:"취소 또는 실패"}[r.status];
    tr.append(element("td",finite(r.time)?new Date(r.time*1000).toLocaleString("ko-KR"):index===0?"가장 최근 · 시각 없음":`${index+1}번째 · 시각 없음`),element("td",label),
      element("td",`${fixed(r.prompt_tokens,0)} / ${fixed(r.generated_tokens,0)}`),
      element("td",fixed(r.cached_prompt_tokens,0)),
      element("td",`${fixed(r.ttft_seconds,2)} / ${fixed(r.request_seconds,2)} s`),
      element("td",`${fixed(r.prefill_seconds,2)} / ${fixed(r.decode_seconds,2)} s`),
      element("td",`${fixed(prefillSpeed(r))} / ${fixed(r.decode_tokens_per_second)} tok/s`));
    return tr;
  }));
  if(!rows.length){
    const tr=element("tr"),td=element("td","아직 요청 기록이 없습니다.");td.colSpan=7;tr.append(td);$("request-history").append(tr);
  }
  const tuning=s.job.state==="running"&&s.job.action==="/api/tune", totalMemory=finite(s.hardware?.memory_gib)&&s.hardware.memory_gib>0?s.hardware.memory_gib*1024**3:null;
  const meter=$("memory-meter"), measured=s.engine.running&&memory?.state==="measured"&&finite(memory.rss_bytes);
  meter.hidden=!(measured&&totalMemory);
  if(!meter.hidden){
    meter.max=totalMemory;meter.value=memory.rss_bytes;
    const detail=`엔진 RSS ${fixed(memory.rss_bytes/1024**3,1)} / 물리 메모리 ${fixed(s.hardware.memory_gib,1)} GiB. RSS 합계는 공유 메모리를 중복 집계할 수 있습니다.`;
    meter.setAttribute("aria-valuetext",detail);meter.title=detail;
    $("memory-capacity").textContent=`${fixed(memory.rss_bytes/1024**3,1)} / ${fixed(s.hardware.memory_gib,1)}`;
    $("memory-capacity").title=detail;
  }else{
    $("memory-capacity").textContent=measured?"총 용량 미확인":s.engine.running?"측정 대기":"중지됨";
    $("memory-capacity").title="";
  }
  // Bars are fixed clock slots aggregated by the server, so their spacing is time, not request order.
  const series=s.monitor?.request_series, buckets=Array.isArray(series?.buckets)?series.buckets:null;
  const mode=!buckets&&series!==null?"restart":buckets&&s.engine.running?"series":"stopped";
  const seconds=finite(series?.bucket_seconds)&&series.bucket_seconds>0?series.bucket_seconds:300;
  const slots=Array.from({length:CHART_SLOTS},(_,i)=>buckets?.[buckets.length-CHART_SLOTS+i]);
  const charts={
    decode:renderRequestChart("decode","생성 속도",mode,slots,seconds),
    prefill:renderRequestChart("prefill","입력 처리 속도",mode,slots,seconds),
    ttft:renderRequestChart("ttft","첫 응답 대기",mode,slots,seconds),
  };
  publishMonitor({state:tuning?"tuning":s.engine.ready?"ready":s.engine.running?"loading":Number.isInteger(s.engine.exit_code)?"error":s.engine.installed&&s.engine.models?"stopped":"setup",
    charts});
}
function renderTuning(s) {
  const t=historyPreview||s.tuning||{}, capable=s.capabilities?.tune_apply===true&&!historyPreview;
  const errorRun=s.tuning?.tuning_id??null;
  if(errorRun!==tuningErrorRun){tuningErrorRun=errorRun;tuningRequestError="";}
  renderTuningError();
  const running=t.state==="running", pending=capable&&t.state==="done"&&t.pending===true&&t.applied===false;
  const before=t.settings_before, after=t.proposal||t.settings_after||before;
  const changed=!!before&&!!after&&JSON.stringify(before)!==JSON.stringify(after);
  const verification=t.verification;
  if(!historyPreview)$("tune-pending").classList.toggle("hidden",!pending);
  $("tuning-title").textContent=historyPreview?"보관된 튜닝 기록 · 읽기 전용":running?"자동 튜닝 · 측정 중":t.state==="done"?(t.current_only?"현재값 측정 완료":t.recovered?"기본값을 기준으로 복구 제안을 준비했습니다.":verification?.state==="rejected"?"최종 검증에서 worker·chunk 채택을 보류했습니다.":changed?(t.adopted?"더 빠른 값을 찾았습니다.":"설정 제안을 준비했습니다."):"채택 기준을 넘는 개선이 없습니다."):"자동 튜닝 · "+({cancelled:"취소됨",error:"오류",restore_error:"복원 오류"}[t.state]||"아직 측정 전");
  const p=t.progress;
  const detail=p?`${tuningPhase(p.phase)} · 시험 ${p.trial}/${p.total} · ${p.detail}${p.candidate?" · "+p.candidate.workers.join("/")+" @ "+p.candidate.chunk:""}`:s.job?.action==="/api/tune"?s.job.message:"";
  $("tuning-status").textContent=[detail,t.error,t.apply_error,t.restoration_error?"복원 실패: "+t.restoration_error:""].filter(Boolean).join(" · ");
  $("tuning-progress").classList.toggle("hidden",!running);
  $("tuning-progress").max=p?.total||1;$("tuning-progress").value=p?.completed||0;
  const elapsed=p?Math.max(0,Math.floor(((t.finished_at||Date.now()/1000)-p.started_at))):null;
  $("tuning-timing").textContent=elapsed==null?"":`경과 ${Math.floor(elapsed/60)}분 ${elapsed%60}초${running&&p.eta_seconds!=null?` · 완료한 후보 소요시간 기준 예상 잔여 ${Math.ceil(p.eta_seconds/60)}분`:""}`;
  const legacy=historyPreview?"보관 기록은 자동 적용하지 않습니다. 현재 환경에서 재측정한 뒤 적용하세요.":!capable?"이 서버는 성공 시 자동 적용합니다 — 새 적용 흐름은 서버 재시작 후 사용됩니다.":t.state==="done"&&!Object.hasOwn(t,"applied")?"이전 방식으로 적용됨":pending?(t.restoration_error?"적용 대기 중 · 복원 오류를 확인하세요.":"적용 대기 중 · 원래 설정과 실행 상태를 복원했습니다."):t.applied?"사용자 확인 후 적용됨":"";
  if(!historyPreview)$("tune-state").textContent=[running?detail:t.state?`${t.state} · ${legacy}`:"아직 측정한 결과가 없습니다.",verification?.reason,t.error,t.apply_error,t.restoration_error].filter(Boolean).join(" · ");
  const results=t.results||[], workers=results.filter(r=>["workers","fallback_baseline"].includes(r.phase||"workers")), chunks=results.filter(r=>r.phase==="chunk");
  const recovered=t.fallback?.state==="used", workerBaseline=recovered?workers.find(r=>r.phase==="fallback_baseline"):workers[0];
  const selectedWorkers=workers.find(r=>r.score!=null&&JSON.stringify(r.candidate.workers)===JSON.stringify(after?.workers));
  const selectedChunk=chunks.find(r=>r.score!=null&&r.candidate.chunk===after?.chunk&&JSON.stringify(r.candidate.workers)===JSON.stringify(after?.workers));
  const finalBaseline=results.find(r=>r.phase==="verify_baseline"), finalCandidate=results.find(r=>r.phase==="verify_candidate");
  const finalComparison=verification&&["running","passed","rejected","error","cancelled"].includes(verification.state);
  const finalMetric=(r,key)=>r?.score!=null&&!r.error?r[key]:null;
  const speedRows=finalComparison
    ?[["읽기 속도 · 최종 조합 재측정",finalMetric(finalBaseline,"prefill_score"),finalMetric(finalCandidate,"prefill_score")],
      ["생성 속도 · 최종 조합 재측정",finalMetric(finalBaseline,"decode_score"),finalMetric(finalCandidate,"decode_score")]]
    :[["읽기 속도 · 읽기 묶음 단계",chunks[0]?.score,selectedChunk?.score],["생성 속도 · 워커 단계",workerBaseline?.score,selectedWorkers?.score]];
  let near=false;
  $("tuning-speeds").replaceChildren(...speedRows.map(([label,oldValue,newValue])=>{
    const delta=Number.isFinite(oldValue)&&Number.isFinite(newValue)&&oldValue>0?(newValue/oldValue-1)*100:null;
    if(delta>3&&delta<5)near=true;
    const row=element("tr");row.append(element("td",label),element("td",speed(oldValue)),element("td",speed(newValue)),element("td",delta==null?"—":`${delta>=0?"+":""}${delta.toFixed(1)}%`));return row;
  }));
  const baselineCheck=t.baseline_check;
  $("tuning-warning").textContent=[legacy,
    recovered?"원래 workers로 시작하지 못했습니다. 속도 표의 비교 기준은 rec 기본 36/32/32이며, 원래 설정 대비 성능 개선은 확인하지 못했습니다.":"",
    baselineCheck?`기준 응답 재현성 ${baselineCheck.completed}/${baselineCheck.required}회 · ${{pending:"확인 중",passed:"통과",failed:"확인 실패"}[baselineCheck.state]||baselineCheck.state}`:"",
    verification?.reason,
    verification?.state==="rejected"&&pending?"worker·chunk는 유지하며 별도의 캐시 용량 제안만 적용 대기 중입니다.":"",
    finalComparison?"전/후는 측정 기준과 최종 후보 조합을 각각 새로 시작해 재측정한 값입니다. 이 비교는 측정용 문맥·캐시 꺼짐 조건이며 다른 작업에서의 성능을 보장하지 않습니다.":"전/후는 각 출처 단계에서 실제로 잰 값입니다. 최종 워커+청크 조합의 통합 측정값이 아니며 성능을 보장하지 않습니다.",
    t.state&&!baselineCheck?"이전 결과에는 새 기준 응답 재현성 확인·최종 조합 검증 기록이 없습니다.":"",
    near?"개선폭이 채택 기준과 가깝습니다. 실행마다 측정값이 변동할 수 있습니다.":"",
    t.cache_selection?.minimum_fallback?"여유 디스크 20% 예산 미달 · 원본 규칙에 따라 최소 캐시 8 GiB를 선택했습니다.":""].filter(Boolean).join(" ");
  $("tuning-trials").replaceChildren(...results.map(r=>{
    const finalTrial=r.phase==="verify_baseline"||r.phase==="verify_candidate";
    const original=JSON.stringify(r.candidate.workers)===JSON.stringify(before?.workers)&&r.candidate.chunk===before?.chunk;
    const chosen=r.score!=null&&(finalTrial?r===finalCandidate&&verification?.state==="passed":r.phase==="chunk"?r===selectedChunk:r===selectedWorkers);
    const tr=element("tr",undefined,chosen?"tuning-chosen":"");
    const group=name=>median((r.rounds||[]).filter(v=>v.group===name).map(v=>v.decode_tokens_per_second));
    const outcome=chosen?(pending?"✓ 제안값":t.applied?"✓ 적용값":"✓ 선택값"):trialStatus(r,t);
    tr.append(element("td",`${tuningPhase(r.phase)} ${r.candidate.workers.join("/")} @ ${r.candidate.chunk.toLocaleString()}${original?" (지금 값)":""}`),
      element("td",speed(group("short"))),element("td",speed(group("long"))),element("td",speed(r.prefill_score)),
      element("td",outcome+retryNote(r)));return tr;
  }));
  $("tuning-settings").replaceChildren(...(before&&after?[["워커","workers"],["읽기 묶음","chunk"],["문맥","context"],["대화 캐시 · GiB","cache_gb"]].map(([label,key])=>{
    const format=v=>Array.isArray(v)?v.join(", "):v?.toLocaleString()??"—", row=element("tr");
    row.append(element("td",label),element("td",format(before[key])),element("td",format(after[key])),element("td",JSON.stringify(before[key])===JSON.stringify(after[key])?"그대로":"바뀜"));return row;
  }):[]));
  const env=t.environment;
  $("tuning-environment").textContent=historyPreview
    ?`보관된 환경 · ${env?.chip??"알 수 없음"} · GPU ${env?.gpu_cores??"—"} · ${env?.memory_gib??"—"} GiB · macOS ${env?.macos??"—"} · 설정 문맥 ${t.context?.toLocaleString()??"—"}`
    :$("tune-environment").textContent;
  $("tuning-later").classList.toggle("hidden",!pending);
  $("tuning-apply").classList.toggle("hidden",!pending);
  $("tuning-apply").disabled=applyingTuning||s.job.state==="running";
  $("tuning-cancel").classList.toggle("hidden",!running||!!historyPreview);
  $("tuning-cancel").disabled=s.job.action==="/api/tune/apply"||p?.phase==="restore";
  const id=t.tuning_id||(running?"legacy-running":"");
  if(running&&id!==seenTuning&&!historyPreview){seenTuning=id;openTuning();}
  if(!running)seenTuning="";
}
function render() {
  const s=state,e=s.engine,busy=s.job.state==="running";
  const tuning=busy&&s.job.action==="/api/tune";
  $("engine-state").textContent=tuning?"튜닝 중 · 클라이언트 일시 중지":e.ready?"실행 중 · 준비 완료":e.running?"모델 로드 중":e.installed?"중지됨":"설치 필요";
  $("engine-state").classList.toggle("ready",e.ready);
  $("engine-description").textContent=tuning?"독점 측정 중에는 외부 CLI 요청을 거부합니다. 완료 후 원래 실행 상태로 복원합니다.":e.ready?"로컬 API가 준비되었습니다. 클라이언트 전용 실행기로 연결하세요.":e.running?"모델을 메모리에 로드하고 있습니다. 잠시 기다려 주세요.":e.installed?"준비되면 서버를 시작하세요. 다른 추론 엔진은 자동으로 중지하지 않습니다.":"엔진과 모델을 준비한 후 서버를 시작할 수 있습니다.";
  const osCheck=s.hardware?.macos_check, osBlocked=!!osCheck&&osCheck.passed!==true;
  $("setup-requirement").textContent=osBlocked?`${osCheck.name}: ${osCheck.reason}`:"";
  $("setup-requirement").classList.toggle("hidden",!osBlocked);
  $("start").disabled=busy||e.running||!e.installed||!e.models||osBlocked||s.hardware?.workers_status.state==="invalid";$("stop").disabled=busy||!e.running;$("tune").disabled=busy||!e.installed||!e.models||!s.tuning_plan?.allowed;
  $("baseline").disabled=$("tune").disabled;
  $("api-base").textContent=s.api_base;$("api-detail").textContent=s.api_base;
  $("installed").textContent=e.installed?"패치 런타임 준비됨":"미설치";$("models").textContent=e.verified?"모델 무결성 확인됨":e.models?"모델 있음 · 검증 필요":"모델 없음";
  $("data-dir").textContent=s.data_dir;
  const gb=(s.cache_bytes/1024**3).toFixed(2);$("cache-size").replaceChildren(document.createTextNode(gb+" "),element("small","GiB"));$("cache-total").textContent=gb+" GiB";
  $("cache-description").textContent=s.settings.disk_cache?"사용 설정됨 · 반복 접두사 TTFT 개선":"꺼짐 · 기존 캐시는 자동 삭제하지 않음";
  const last=[...s.metrics].reverse().find(r=>r.status==="complete");
  $("decode").replaceChildren(document.createTextNode(finite(last?.decode_tokens_per_second)?last.decode_tokens_per_second.toFixed(1):"—"),element("small"," tok/s"));
  $("ttft").replaceChildren(document.createTextNode(finite(last?.ttft_seconds)?last.ttft_seconds.toFixed(2):"—"),element("small"," s"));
  $("job").classList.toggle("hidden",!busy);$("job-action").textContent=actions[s.job.action]||"로컬 작업";$("job-message").textContent=s.job.message;
  const jobKey=JSON.stringify(s.job);
  if(jobKey!==previousJob&&!busy&&s.job.state!=="idle")notify(s.job.state==="error"?s.job.message:s.job.state==="cancelled"?"작업이 취소되었습니다.":"작업 완료",s.job.state==="error");
  previousJob=jobKey;
  $("logs").textContent=s.logs.map(l=>new Date(l.time*1000).toLocaleTimeString("ko-KR")+"  "+l.message).join("\n");
  renderHardware(s);
  if($("calibration-plan"))$("calibration-plan").textContent=s.calibration?`현재 문맥 ${s.settings.context.toLocaleString()}토큰 유지 · 데이터 볼륨 여유 디스크 기준 캐시 ${s.calibration.cache_gb} GiB 예상. 전체 측정이 끝날 때 다시 계산하며, 아직 저장된 값이 아닙니다.`:"이 기능의 설명을 표시하려면 웹 서버를 재시작하세요.";
  const cacheSelection=s.tuning.cache_selection, applied=s.tuning.settings_after;
  $("tune-state").textContent=s.tuning.state?`${s.tuning.state} · ${s.tuning.adopted?"3% 초과 성능 후보 채택":"worker·chunk 기존값 유지"}${applied?` · 저장 문맥 ${applied.context.toLocaleString()}토큰 · 캐시 ${applied.cache_gb} GiB (${applied.disk_cache?"사용":"꺼짐"})`:""}${cacheSelection?` · 디스크 기준 선택 ${cacheSelection.selected_gb} GiB ${cacheSelection.applied?"적용":"미적용"}${cacheSelection.minimum_fallback?" (20% 예산 미달 · 최소값 선택)":""}`:""}${s.tuning.error?" · "+s.tuning.error:""}${s.tuning.restoration_error?" · 복원 실패: "+s.tuning.restoration_error:""}`:"아직 측정한 결과가 없습니다.";
  $("tune-results").replaceChildren(...(s.tuning.results||[]).map(r=>{const tr=element("tr");tr.append(element("td",tuningPhase(r.phase)+" · "+r.candidate.workers.join("/")+" · "+r.candidate.chunk),element("td",r.score?.toFixed(2)??"—"),element("td",trialStatus(r,s.tuning)+retryNote(r)));return tr;}));
  document.querySelectorAll("[data-action]").forEach(b=>b.disabled=busy||e.running||
    osBlocked&&["/api/install","/api/models/pull","/api/models/verify"].includes(b.dataset.action));
  $("settings-form").querySelectorAll("input,select,button").forEach(b=>b.disabled=busy||e.running);
  fillSettings();renderClients();renderHistory(s);renderTuning(s);renderMonitor(s);
}
async function refresh() {
  if(polling)return;polling=true;
  let seconds;
  try {
    for(;;){
      seconds=chartSeconds;
      const selectable=supportsChartIntervals(state);
      const next=await api(selectable&&seconds!==300?`/api/state?chart_seconds=${seconds}`:"/api/state");
      // A slower response must not undo a newer choice. Discover support before sending queries to old servers.
      if(seconds!==chartSeconds)continue;
      state=next;
      if(!selectable&&supportsChartIntervals(next)&&seconds!==300)continue;
      $("connection").classList.add("hidden");render();break;
    }
  }
  catch(e){$("connection").textContent="연결 오류: "+e.message+" · 표시된 수치는 마지막 조회값입니다.";$("connection").classList.remove("hidden");$("start").disabled=true;$("stop").disabled=true;$("memory-label").textContent="연결 끊김 · 이전 조회값";$("memory-meter").hidden=true;$("memory-capacity").textContent="이전 조회값";chartStatus("집계 갱신 실패 · 표시된 차트와 시간축은 마지막 조회값입니다.");publishMonitor({state:"offline"});if($("tuning-dialog").open){tuningRequestError="연결 오류: "+e.message;renderTuningError();}}
  finally{polling=false;}
  if(seconds!==chartSeconds)await refresh();
}
async function boot() {
  try {chartSeconds=readChartSeconds();const response=await fetch("/api/session",{credentials:"same-origin"});if(!response.ok)throw Error("세션을 만들 수 없습니다.");csrf=(await response.json()).csrf;await refresh();setInterval(refresh,2000);}
  catch(e){notify(e.message,true);}
}
boot();
