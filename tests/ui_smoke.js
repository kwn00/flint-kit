"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(text="") {
    this.textContent=text;this.children=[];this.value="";this.disabled=false;
    this.classes=new Set();
    this.classList={toggle:(name,on)=>{if(on)this.classes.add(name);else this.classes.delete(name);},
      add:name=>this.classes.add(name),remove:name=>this.classes.delete(name),contains:name=>this.classes.has(name)};
    this.fields=new Map();
    this.dataset={};this.style={};this.attributes={};
    this.open=false;this.listeners={};
    this.elements={namedItem:name=>{if(!this.fields.has(name))this.fields.set(name,new Element());return this.fields.get(name);}};
  }
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  querySelectorAll(){return [];}
  querySelector(selector){
    return this.children.find(e=>selector.startsWith(".")?(e.className||"").split(" ").includes(selector.slice(1)):e.tagName===selector.toUpperCase())
      ||this.children.map(e=>e.querySelector(selector)).find(Boolean)||null;
  }
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  showModal(){this.open=true;}
  close(){this.open=false;}
  focus(){}
}
const elements=new Map();
const storage=new Map();
const windowListeners={}, dispatched=[];
const window={localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)},
  addEventListener(name,fn){(windowListeners[name]??=[]).push(fn);},
  dispatchEvent(event){dispatched.push(event);(windowListeners[event.type]||[]).forEach(fn=>fn(event));return true;}};
class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail;}}
const document={
  documentElement:{dataset:{}},
  getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  createElement(tag){const e=new Element();e.tagName=tag.toUpperCase();return e;},
  createTextNode(text){return new Element(text);},
  querySelectorAll(){return [];},
};
const sandbox=vm.createContext({document,window,CustomEvent,console,fetch:()=>new Promise(()=>{}),setInterval(){}});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../static/app.js"),"utf8"),sandbox);
const settings={context:65536,port:11540,workers:[36,32,32],chunk:2048,disk_cache:false,
  cache_gb:8,temperature:1,top_p:0.95,top_k:20,presence_penalty:0,output_limit:8192};
const state={settings,engine:{installed:true,models:true,verified:true,running:true,ready:true},
  job:{state:"running",action:"/api/tune",message:"현재 측정 중"},metrics:[],logs:[],cache_bytes:0,
  api_base:"http://127.0.0.1:11540/v1",data_dir:"/isolated",free_bytes:100*1024**3,
  tuning:{state:"running",results:[{phase:"workers",candidate:{workers:[36,32,32],chunk:2048},score:100}]},
  candidates:[{workers:[36,32,32],chunk:2048}],integrations:{}};
function render(value){sandbox.input=JSON.stringify(value);vm.runInContext("state=JSON.parse(input);render();",sandbox);}
function text(id){return elements.get(id).textContent;}
function allText(element){return [element.textContent,...element.children.map(allText)].join(" ");}

// Old backend + new assets: preserve progress/results/cancel without guessed hardware.
render(state);
assert.match(allText(elements.get("hardware-details")),/웹 서버를 재시작/);
assert.equal(elements.get("tune").disabled,true);
assert.equal(elements.get("baseline").disabled,true);
assert.equal(elements.get("candidate-list").children.length,0);
assert.equal(elements.get("tune-results").children.length,1);
assert.equal(elements.get("job-message").textContent,"현재 측정 중");
assert.equal(elements.get("cancel").disabled,false);
assert.equal(text("tune-environment"),"측정 환경 기록 없음");
assert.equal(elements.get("settings-form").elements.namedItem("workers").value,"36,32,32");

const hardware={chip:"Apple M4 Pro",gpu_cores:20,memory_gib:48,free_bytes:100*1024**3,reasons:{},
  checks:[{name:"Apple Silicon",passed:true,reason:""}],defaults:{workers:[36,32,32],chunk:2048},
  worker_options:[[32,32,32],[36,32,32]],workers_status:{state:"valid",reason:""}};
const current={...state,hardware,tuning_plan:{workers:[[36,32,32],[32,32,32]],chunks:[1024,2048,4096],
  worker_count:2,chunk_count:3,allowed:true,reason:""},job:{state:"idle"},tuning:{state:"done",results:[],
  environment:{chip:"Apple M4 Pro",gpu_cores:20,memory_gib:48,macos:"27.2"}},tuning_environment_status:"same"};
render(current);
assert.match(text("hardware-title"),/Apple M4 Pro/);
assert.match(allText(elements.get("hardware-details")),/48 GiB/);
assert.equal(elements.get("candidate-list").children.length,2);
assert.match(text("candidate-description"),/worker 단계 2개 \+ chunk 단계 3개/);
assert.equal(elements.get("tune").disabled,false);
assert.equal(elements.get("settings-form").elements.namedItem("workers").children.length,2);
assert.match(text("tune-environment"),/macOS 27.2/);
assert.match(text("tuning-reliability"),/웹 서버를 재시작/);
assert.doesNotMatch(allText(elements.get("hardware-details")),/macOS/);
render({...current,tuning_environment_status:"different"});
assert.match(text("tune-environment"),/재측정 권장/);
const unknown={...current,hardware:{...hardware,gpu_cores:null,reasons:{gpu_cores:"ioreg timeout"},
  workers_status:{state:"unverified",reason:"현재 설정 검증 불가"},worker_options:[[36,32,32]]},
  tuning_plan:{...current.tuning_plan,allowed:false,reason:"GPU 코어 알 수 없음",workers:[],worker_count:0}};
render(unknown);
assert.match(allText(elements.get("hardware-details")),/알 수 없음 · ioreg timeout/);
assert.equal(elements.get("tune").disabled,true);
assert.equal(elements.get("start").disabled,true); // Engine already running, not because of unknown cores.
render({...unknown,engine:{...unknown.engine,running:false,ready:false}});
assert.equal(elements.get("start").disabled,false);
render({...current,engine:{...current.engine,running:false,ready:false},
  hardware:{...hardware,macos_check:{name:"macOS 15 이상",passed:false,reason:"현재 macOS 14.7"}}});
assert.equal(elements.get("start").disabled,true);
assert.match(text("setup-requirement"),/macOS 14.7/);
render({...current,hardware:{...hardware,macos_check:{name:"macOS 15 이상",passed:true,reason:""}}});
assert.equal(elements.get("setup-requirement").classes.has("hidden"),true);
render({...current,settings:{...settings,workers:[76,64,64]},
  hardware:{...hardware,workers_status:{state:"invalid",reason:"다른 Mac 설정"}},
  engine:{...current.engine,running:false,ready:false}});
assert.equal(elements.get("start").disabled,true);
assert.equal(elements.get("settings-form").elements.namedItem("workers").value,"76,64,64");
console.log("UI render smoke passed: old/new state, unknown hardware, invalid workers, environment, cancel preserved");

const connection={enabled:false,path:"/example/pi/models.json",can_connect:true,can_remove:false,
  needs_update:false,error:"",backup:""};
const clients={...current,integrations:{
  pi:{enabled:false,connection,alias:{enabled:false}},
  opencode:{enabled:false,connection:{...connection,path:"/example/opencode.jsonc"},alias:{enabled:false}},
  claude:{enabled:false,command:"/example/claude/launch",
    alias:{enabled:false,command:"claude-flint",path:"/example/bin/claude-flint",on_path:false}},
  codex:{enabled:false,command:"/example/codex/launch",
    alias:{enabled:false,command:"codex-flint",path:"/example/bin/codex-flint",on_path:true}},
}};
function descendants(element){return element.children.flatMap(e=>[e,...descendants(e)]);}
function clientButton(index,label){
  return descendants(elements.get("client-list").children[index]).find(e=>e.tagName==="BUTTON"&&e.textContent===label);
}
render(clients);
assert.equal(elements.get("client-list").children.length,4);
for(const card of elements.get("client-list").children){
  assert.equal(card.className,"client-card");
  assert.equal(card.querySelector(".client-details").open,false);
  assert.equal(card.children.some(e=>e.tagName==="PRE"),false,"paths and long commands stay in collapsed details");
  assert.match(allText(card.querySelector(".client-status")),/미설정/);
}
assert.match(allText(elements.get("client-list")),/평소 pi의 \/model/);
assert.match(allText(elements.get("client-list")),/\/models/);
assert.match(allText(elements.get("client-list")),/claude는 기존 Claude/);
assert.equal(clientButton(0,"기존 CLI에 Flint 추가").disabled,false);
assert.equal(clientButton(0,"전용 실행기 생성"),undefined);
assert.equal(clientButton(2,"claude-flint 명령 설치").disabled,true);
assert.equal(clientButton(3,"codex-flint 명령 설치").disabled,true);
const firstCard=elements.get("client-list").children[0], firstInfo=firstCard.querySelector(".client-details");
firstInfo.open=true;
render({...clients,metrics:[{status:"complete",time:123,decode_tokens_per_second:25}]});
assert.equal(elements.get("client-list").children[0],firstCard,"unrelated status polls preserve client DOM and focus");
assert.equal(firstInfo.open,true);
const connectedClients={...clients,integrations:{...clients.integrations,
  pi:{...clients.integrations.pi,connection:{...connection,enabled:true,can_remove:true,needs_update:true}},
  opencode:{...clients.integrations.opencode,
    connection:{...clients.integrations.opencode.connection,enabled:true,can_remove:true,needs_update:true}}}};
render(connectedClients);
assert.equal(elements.get("client-list").children[0].querySelector(".client-details").open,true,"details survive a connection change");
for(const index of [0,1]){
  const row=elements.get("client-list").children[index].children.find(e=>e.className==="button-row");
  assert.ok(row,"Pi/opencode connection buttons use the shared wrapping, spaced button row");
  assert.deepEqual(row.children,[clientButton(index,"Flint 설정 갱신"),clientButton(index,"Flint 연결 해제")]);
  assert.equal(clientButton(index,"Flint 설정 갱신").disabled,false);
  assert.equal(clientButton(index,"Flint 연결 해제").disabled,false);
}
assert.match(allText(elements.get("client-list")),/포트·문맥·출력 한도가 변경/);
assert.ok(elements.get("client-list").children[0].children.some(e=>e.className==="client-alert"),"update guidance stays outside collapsed details");
render({...connectedClients,job:{state:"running"}});
assert.equal(clientButton(0,"Flint 설정 갱신").disabled,true);
assert.equal(clientButton(0,"Flint 연결 해제").disabled,true);
assert.equal(elements.get("client-list").children[0].querySelector(".client-details").open,true);
render({...clients,integrations:{pi:{enabled:false}}});
assert.match(allText(elements.get("client-list")),/웹 서버를 재시작/);
render({...clients,integrations:{pi:{...clients.integrations.pi,
  connection:{...connection,error:"Flint 항목이 외부에서 수정됨",can_connect:false}}}});
assert.equal(clientButton(0,"기존 CLI에 Flint 추가").disabled,true);
assert.match(allText(elements.get("client-list")),/외부에서 수정/);
assert.match(allText(elements.get("client-list").children[0].children.find(e=>e.className==="client-alert")),/외부에서 수정/);
const preparedClients={...clients,integrations:{...clients.integrations,
  claude:{...clients.integrations.claude,enabled:true},
  codex:{...clients.integrations.codex,enabled:true,alias:{...clients.integrations.codex.alias,enabled:true}}}};
render(preparedClients);
assert.equal(clientButton(2,"claude-flint 명령 설치").disabled,false);
assert.equal(clientButton(3,"실행기 복원 / 제거").disabled,true,"remove the alias before the launcher");
assert.equal(clientButton(3,"전용 명령 제거").disabled,false);
for(const index of [2,3]){
  const card=elements.get("client-list").children[index];
  assert.equal(card.querySelector(".button-row").children.length,2,"launcher and alias controls share a spaced row");
  assert.equal(card.querySelector(".client-details").open,false);
  assert.match(allText(card.querySelector(".client-status")),/준비됨/);
}
const legacyClients={...connectedClients,integrations:{...connectedClients.integrations,
  pi:{...connectedClients.integrations.pi,enabled:true,command:"/example/pi/launch",
    alias:{enabled:true,command:"pi-flint",path:"/example/bin/pi-flint",on_path:false}}}};
render(legacyClients);
assert.equal(clientButton(0,"실행기 복원 / 제거").disabled,true);
assert.equal(clientButton(0,"전용 명령 제거").disabled,false);
const legacyInfo=elements.get("client-list").children[0].querySelector(".client-details");
assert.match(allText(legacyInfo),/기존 격리 실행기/);
assert.match(allText(legacyInfo),/export PATH/);
assert.ok(descendants(legacyInfo).includes(clientButton(0,"전용 명령 제거")),"legacy controls remain inside details");
console.log("Compact client smoke passed: four cards, progressive disclosure, poll persistence, alerts, launcher/alias controls");

const before={...settings,chunk:1024,cache_gb:16};
const after={...before,workers:[32,32,32]};
const rounds=(short,long)=>[...short.map(v=>({group:"short",decode_tokens_per_second:v})),
  ...long.map(v=>({group:"long",decode_tokens_per_second:v}))];
const results=[
  {phase:"workers",candidate:{workers:before.workers,chunk:1024},score:94.46,
    rounds:rounds([119.9,121.3,121.8],[58.6,68.2,67.6]),prefill_score:779},
  {phase:"workers",candidate:{workers:after.workers,chunk:1024},score:98.42,
    rounds:rounds([129.1,129.7,129.9],[64.9,68.1,67.1]),prefill_score:785.4},
  ...[[1024,780.68],[2048,778],[4096,694.56]].map(([chunk,score])=>({phase:"chunk",
    candidate:{workers:after.workers,chunk},score,prefill_score:score})),
  {phase:"chunk",candidate:{workers:after.workers,chunk:2048},score:null,error:"greedy 검증 실패"},
];
const pending={...current,capabilities:{tune_apply:true},settings:before,tuning:{state:"done",tuning_id:"fixture",
  pending:true,applied:false,adopted:true,proposal:after,settings_before:before,settings_after:after,results}};
render(pending);
assert.match(text("tuning-title"),/더 빠른 값/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),false);
assert.equal(elements.get("tune-pending").classList.contains("hidden"),false);
assert.match(allText(elements.get("tuning-speeds")),/워커 단계.*94.5 tok\/s.*98.4 tok\/s.*\+4.2%/);
assert.match(allText(elements.get("tuning-speeds")),/읽기 묶음 단계.*780.7 tok\/s/);
assert.match(allText(elements.get("tuning-trials")),/지금 값/);
assert.match(allText(elements.get("tuning-trials")),/실패 \/ 제외: greedy 검증 실패/);
assert.match(allText(elements.get("tuning-settings")),/워커.*36, 32, 32.*32, 32, 32.*바뀜/);
assert.match(text("tuning-warning"),/변동할 수/);
elements.get("tune-view").onclick();
assert.equal(elements.get("tuning-dialog").open,true);
elements.get("tuning-later").onclick();
assert.equal(elements.get("tuning-dialog").open,false);
assert.equal(elements.get("tune-pending").classList.contains("hidden"),false);
elements.get("tune-view").onclick();
let prevented=false;
elements.get("tuning-dialog").listeners.cancel({preventDefault(){prevented=true;}});
assert.equal(prevented,true);
assert.equal(elements.get("tuning-dialog").open,false);

const progress={...pending,job:{state:"running",action:"/api/tune",message:"측정 중"},tuning:{...pending.tuning,
  state:"running",pending:false,tuning_id:"running",results:results.slice(0,1),
  progress:{phase:"workers",trial:2,total:5,completed:1,detail:"짧은 생성 2/3",candidate:{workers:[32,32,32],chunk:1024},
    started_at:Date.now()/1000-60,updated_at:Date.now()/1000,eta_seconds:240}}};
render(progress);
assert.equal(elements.get("tuning-dialog").open,true);
assert.match(text("tuning-status"),/시험 2\/5.*짧은 생성 2\/3/);
assert.equal(elements.get("tuning-progress").value,1);
assert.match(text("tuning-timing"),/예상 잔여 4분/);
assert.equal(elements.get("tuning-trials").children.length,1);
elements.get("tuning-close").onclick();
render(progress);
assert.equal(elements.get("tuning-dialog").open,false); // Polling does not reopen a dismissed run.
render({...pending,tuning:{...pending.tuning,proposal:before,settings_after:before,pending:false,adopted:false}});
assert.equal(text("tuning-title"),"채택 기준을 넘는 개선이 없습니다.");
render({...pending,tuning:{...pending.tuning,proposal:before,settings_after:before,pending:false,adopted:false,
  results:[results[0],{...results[1],score:results[0].score*1.02,
    rounds:results[0].rounds.map(row=>({...row,decode_tokens_per_second:row.decode_tokens_per_second*1.02}))},...results.slice(2)]}});
assert.equal(text("tuning-title"),"채택 기준을 넘는 개선이 없습니다.");
assert.match(allText(elements.get("tuning-trials")),/측정 완료/); // Faster 2% candidate remains unadopted.
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
assert.equal(elements.get("tuning-later").classList.contains("hidden"),true);
render({...pending,capabilities:undefined});
assert.match(text("tuning-warning"),/성공 시 자동 적용/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
render({...pending,tuning:{state:"done",results:[]}});
assert.match(text("tuning-warning"),/이전 방식으로 적용됨/);
for(const status of ["cancelled","error","restore_error"]){
  render({...pending,tuning:{state:status,error:"원인 설명",restoration_error:status==="restore_error"?"재시작 실패":undefined}});
  assert.match(text("tuning-status"),/원인 설명/);
  if(status==="restore_error")assert.match(text("tuning-status"),/복원 실패: 재시작 실패/);
}
const finalBaseline={phase:"verify_baseline",candidate:{workers:before.workers,chunk:before.chunk},
  score:100,decode_score:100,prefill_score:4000,rounds:rounds([100,100,100],[100,100,100])};
const finalCandidate={phase:"verify_candidate",candidate:{workers:after.workers,chunk:after.chunk},
  score:104,decode_score:104,prefill_score:4100,rounds:rounds([104,104,104],[104,104,104]),
  start_attempts:2,start_errors:["일시적 준비 시간 초과"]};
const verified={...pending,capabilities:{tune_apply:true,tune_reliability:true},
  tuning:{...pending.tuning,baseline_check:{state:"passed",required:3,completed:3},
    verification:{state:"passed",candidate:finalCandidate.candidate,reason:"최종 조합의 개선과 회귀 방지 기준을 통과했습니다."},
    results:[...results,finalBaseline,finalCandidate]}};
render(verified);
assert.match(text("candidate-description"),/최종 비교 2회 추가/);
assert.match(text("tuning-reliability"),/기준 응답 3회.*한 번 재시도.*3% 초과/);
assert.match(allText(elements.get("tuning-speeds")),/생성 속도 · 최종 조합 재측정.*100.0 tok\/s.*104.0 tok\/s.*\+4.0%/);
assert.match(allText(elements.get("tuning-speeds")),/읽기 속도 · 최종 조합 재측정.*4000.0 tok\/s.*4100.0 tok\/s/);
assert.doesNotMatch(allText(elements.get("tuning-speeds")),/98.4 tok\/s|워커 단계/);
assert.match(text("tuning-warning"),/기준 응답 재현성 3\/3회 · 통과/);
assert.doesNotMatch(text("tuning-warning"),/통합 측정값이 아니며|이전 결과/);
assert.match(text("tune-state"),/최종 조합의 개선/);
assert.match(allText(elements.get("tuning-trials")),/엔진 시작 2회 · 이전 실패: 일시적 준비 시간 초과/);
assert.match(allText(elements.get("tune-results")),/최종 검증 통과.*엔진 시작 2회/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),false);

const rejected={...verified,tuning:{...verified.tuning,adopted:false,pending:false,proposal:before,
  settings_after:before,verification:{...verified.tuning.verification,state:"rejected",reason:"생성 속도가 3% 초과 개선을 재현하지 못했습니다."},
  results:[...results,finalBaseline,{...finalCandidate,score:102,decode_score:102}]}};
render(rejected);
assert.match(text("tuning-title"),/최종 검증.*채택을 보류/);
assert.match(allText(elements.get("tuning-speeds")),/100.0 tok\/s.*102.0 tok\/s.*\+2.0%/);
assert.match(text("tuning-warning"),/3% 초과 개선을 재현하지 못했습니다/);
assert.equal(elements.get("tuning-trials").children.at(-1).className,undefined);
assert.match(allText(elements.get("tuning-trials").children.at(-1)),/최종 채택 보류/);
assert.match(allText(elements.get("tune-results")),/최종 채택 보류/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
render({...rejected,tuning:{...rejected.tuning,pending:true,proposal:{...before,cache_gb:32}}});
assert.match(text("tuning-warning"),/별도의 캐시 용량 제안만/);
assert.doesNotMatch(text("tuning-title"),/더 빠른 값/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),false);

for(const phase of ["verify_baseline","verify_candidate"]){
  render({...verified,job:progress.job,tuning:{...verified.tuning,state:"running",pending:false,
    verification:{state:"running"},results:[...results,finalBaseline],
    progress:{...progress.tuning.progress,phase,total:7,trial:phase==="verify_baseline"?6:7,detail:"출력 재현성 2/3"}}});
  assert.match(text("tuning-status"),/최종 비교.*시험 [67]\/7.*출력 재현성 2\/3/);
  assert.equal(elements.get("tuning-cancel").disabled,false);
  assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
}
render({...verified,tuning:{...verified.tuning,state:"error",pending:false,adopted:false,proposal:null,
  verification:{state:"error",reason:"최종 조합 재측정 실패"},
  results:[...results,finalBaseline,{...finalCandidate,score:null,error:"greedy 불일치"}]}});
assert.doesNotMatch(allText(elements.get("tuning-speeds")),/104.0 tok\/s|4100.0 tok\/s/);
assert.match(text("tuning-warning"),/최종 조합 재측정 실패/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
render({...verified,tuning:{...verified.tuning,state:"error",pending:false,adopted:false,proposal:null,
  baseline_check:{state:"failed",required:3,completed:2},verification:{state:"not_run"},
  results:[],error:"기준 응답 재현성을 확인할 수 없습니다."}});
assert.match(text("tuning-warning"),/기준 응답 재현성 2\/3회 · 확인 실패/);
assert.match(text("tuning-status"),/기준 응답 재현성/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
render(pending);
assert.match(text("tuning-warning"),/이전 결과에는.*최종 조합 검증 기록이 없습니다/);
assert.equal(elements.get("tuning-apply").classList.contains("hidden"),false);

const recovery={...verified,capabilities:{...verified.capabilities,tune_recovery:true,tuning_history:true},
  tuning:{...verified.tuning,adopted:false,recovered:true,
    fallback:{state:"used",candidate:{workers:[36,32,32],chunk:2048}},
    verification:{...verified.tuning.verification,reason:"rec 기본값 기준 검증 통과"},
    results:[...verified.tuning.results,{phase:"workers",candidate:{workers:[76,64,64],chunk:2048},
      score:null,skipped:true,skip_reason:"64/64/64 준비 시간 초과보다 큰 값",start_attempts:0}]}};
render(recovery);
assert.match(text("tuning-title"),/기본값.*복구 제안/);
assert.doesNotMatch(text("tuning-title"),/더 빠른 값/);
assert.match(text("tuning-warning"),/rec 기본 36\/32\/32.*원래 설정 대비 성능 개선은 확인하지/);
assert.match(allText(elements.get("tuning-trials")),/건너뜀 \(미측정\).*시간 초과/);
assert.match(text("tuning-recovery"),/현재값만 측정에서는 기본값으로 바꾸지/);

// The server aggregates the current run into clock-aligned 5-minute slots, oldest first; slot 11 is still in progress.
const SLOT_START=1_700_000_100;
const clockLabel=(seconds,withSeconds=false)=>new Date(seconds*1000).toLocaleTimeString("ko-KR",{hour:"2-digit",minute:"2-digit",...(withSeconds?{second:"2-digit"}:{}),hourCycle:"h23"});
const slotWindow=index=>`${clockLabel(SLOT_START+index*300)}–${clockLabel(SLOT_START+index*300+300)}`;
const requestSeries=(filled={})=>({bucket_seconds:300,buckets:Array.from({length:12},(_,i)=>({
  start:SLOT_START+i*300,requests:0,decode:null,prefill:null,ttft:null,...filled[i]}))});
const gaps=values=>Array.from({length:12},(_,i)=>values[i]??null);
const normalSeries=requestSeries({
  2:{requests:3,decode:20,prefill:100,ttft:0.5},
  5:{requests:1,decode:40,prefill:200,ttft:1.5},
  11:{requests:2,decode:10,prefill:50,ttft:0.25}});

async function actionsSmoke(){
  const monitored={...state,job:{state:"idle"},tuning:{},monitor:{engine_started_at:100,
    memory:{state:"measured",rss_bytes:24*1024**3,sampled_peak_bytes:25*1024**3,process_count:2,sampled_at:110},
    errors:[{category:"메모리 할당 오류",lines:2}],request_series:normalSeries},metrics:[
      {status:"complete",time:105,prompt_tokens:100,cached_prompt_tokens:20,prefilled_tokens:80,
        generated_tokens:32,prefill_seconds:0.5,ttft_seconds:0.6,request_seconds:2,decode_tokens_per_second:22.5,peak_mlx_bytes:22*1024**3},
      {status:"cancelled_or_failed",time:106,prompt_tokens:5,generated_tokens:1}]};
  const withSeries=request_series=>({...monitored,monitor:{...monitored.monitor,request_series}});
  const captions=name=>elements.get(`${name}-chart-label`).children.map(e=>e.textContent);
  const barHeight=(name,index)=>Number.parseFloat(elements.get(`${name}-bars`).children[index].children[0].style.height);
  render(monitored);
  const visual=()=>JSON.parse(JSON.stringify(dispatched.filter(e=>e.type==="flint:monitor").at(-1).detail));
  // Bars sit in their clock slot and are scaled to each chart's own maximum; empty slots are gaps.
  assert.deepEqual(visual(),{state:"ready",charts:{
    decode:gaps({2:.5,5:1,11:.25}),prefill:gaps({2:.5,5:1,11:.25}),ttft:gaps({2:.5/1.5,5:1,11:.25/1.5})}});
  const decodeBars=elements.get("decode-bars");
  assert.equal(decodeBars.children.length,12);
  assert.deepEqual([2,5,11,0,7].map(i=>barHeight("decode",i)),[50,100,25,0,0]);
  assert.equal(decodeBars.children[2].title,`${slotWindow(2)} · 요청 3개 · 20 tok/s`);
  assert.equal(decodeBars.children[0].title,`${slotWindow(0)} · 요청 없음`);
  assert.equal(elements.get("prefill-bars").children[5].title,`${slotWindow(5)} · 요청 1개 · 200 tok/s`);
  assert.equal(elements.get("ttft-bars").children[11].title,`${slotWindow(11)} · 요청 2개 · 0.25 s`);
  const decodeLabel=elements.get("decode-chart").attributes["aria-label"];
  assert.match(decodeLabel,/최근 1시간, 5분 단위/);
  assert.ok(decodeLabel.includes(`${clockLabel(SLOT_START+2*300)} 20 tok/s (요청 3개)`));
  assert.match(decodeLabel,/마지막 구간은 진행 중.*생성 시간으로 가중한 평균/);
  assert.doesNotMatch(decodeLabel,/NaN|undefined|null/);
  assert.match(elements.get("prefill-chart").attributes["aria-label"],/캐시.*제외/);
  assert.match(elements.get("ttft-chart").attributes["aria-label"],/0.5 s.*낮을수록 빠릅니다/);
  assert.equal(elements.get("decode-chart").title,"최근 1시간 · 5분 단위");
  assert.equal(elements.get("decode-chart").dataset.empty,"");
  assert.deepEqual(captions("decode"),["1시간 전","5분 단위","지금"]);
  // A poll with unchanged data must not touch the slots, or a tooltip under the pointer would vanish.
  const slotNodes=[...decodeBars.children], captionNodes=elements.get("decode-chart-label").children;
  decodeBars.children[2].title="sentinel";
  render(monitored);
  assert.ok(decodeBars.children.every((slot,i)=>slot===slotNodes[i]));
  assert.equal(elements.get("decode-chart-label").children,captionNodes);
  assert.equal(decodeBars.children[2].title,"sentinel");
  // When the clock rolls into the next slot, the same bars move one place left and new data lands on the right.
  const rolled={bucket_seconds:300,buckets:[...normalSeries.buckets.slice(1),
    {start:SLOT_START+12*300,requests:1,decode:30,prefill:60,ttft:0.75}]};
  render(withSeries(rolled));
  assert.deepEqual(visual().charts.decode,gaps({1:.5,4:1,10:.25,11:.75}));
  assert.ok(decodeBars.children.every((slot,i)=>slot===slotNodes[i]),"slots are updated in place");
  assert.equal(decodeBars.children[1].title,`${slotWindow(2)} · 요청 3개 · 20 tok/s`);
  assert.equal(decodeBars.children[11].title,`${slotWindow(12)} · 요청 1개 · 30 tok/s`);
  assert.equal(barHeight("decode",11),75);
  assert.equal(elements.get("decode-chart").dataset.empty,"");
  assert.equal(elements.get("memory-meter").hidden,true,"unknown total memory never fills a guessed gauge");
  render({...monitored,hardware:{memory_gib:48}});
  assert.equal(elements.get("memory-meter").hidden,false);
  assert.equal(elements.get("memory-meter").value,24*1024**3);
  assert.equal(elements.get("memory-meter").max,48*1024**3);
  assert.equal(text("memory-capacity"),"24 / 48");
  assert.match(elements.get("memory-meter").attributes["aria-valuetext"],/공유 메모리를 중복 집계/);
  render({...monitored,hardware:{memory_gib:0}});
  assert.equal(elements.get("memory-meter").hidden,true);
  render({...monitored,hardware:{memory_gib:48},monitor:{...monitored.monitor,memory:{state:"unavailable",rss_bytes:24*1024**3}}});
  assert.equal(elements.get("memory-meter").hidden,true);
  // A measured zero is a real value; a slot with requests but nothing measurable is a gap with its own explanation.
  render(withSeries(requestSeries({3:{requests:2,decode:12,prefill:null,ttft:0},4:{requests:1}})));
  assert.deepEqual(visual().charts.ttft,gaps({3:0}));
  assert.deepEqual(visual().charts.decode,gaps({3:1}));
  assert.deepEqual(visual().charts.prefill,gaps({}));
  assert.equal(elements.get("ttft-chart").dataset.empty,"");
  assert.equal(elements.get("prefill-chart").dataset.empty,"측정값 없음");
  assert.equal(elements.get("ttft-bars").children[3].title,`${slotWindow(3)} · 요청 2개 · 0 s`);
  assert.equal(elements.get("decode-bars").children[4].title,`${slotWindow(4)} · 요청 1개 · 측정값 없음`);
  assert.equal(barHeight("ttft",3),0);
  render(withSeries(requestSeries({6:{requests:4}})));
  for(const name of ["decode","prefill","ttft"]){
    assert.deepEqual(visual().charts[name],gaps({}));
    assert.equal(elements.get(`${name}-chart`).dataset.empty,"측정값 없음");
    assert.equal(elements.get(`${name}-bars`).children[6].title,`${slotWindow(6)} · 요청 4개 · 측정값 없음`);
    assert.match(elements.get(`${name}-chart`).attributes["aria-label"],/최근 1시간 동안 완료한 요청 4개에 측정값이 없습니다/);
    assert.doesNotMatch(elements.get(`${name}-chart`).attributes["aria-label"],/막대는/);
  }
  assert.match(elements.get("decode-chart").attributes["aria-label"],/한 토큰만 생성해 속도를 잴 수 없는 응답은 제외/);
  render(withSeries(requestSeries()));
  assert.deepEqual(visual().charts.decode,gaps({}));
  assert.equal(elements.get("decode-chart").dataset.empty,"요청 없음");
  assert.match(elements.get("decode-chart").attributes["aria-label"],/최근 1시간 동안 완료한 요청 없음/);
  assert.deepEqual(captions("decode"),["1시간 전","5분 단위","지금"]);
  // Malformed values never reach a bar, a label or the renderer; short or long lists keep the newest 12 slots.
  render(withSeries({bucket_seconds:0,buckets:[null,null,{start:"x",requests:"3",decode:-1,prefill:"5",ttft:true},
    {start:SLOT_START+900,requests:1,decode:12,prefill:null,ttft:null}]}));
  assert.deepEqual(visual().charts.decode,gaps({11:1}));
  assert.deepEqual(visual().charts.prefill,gaps({}));
  assert.equal(decodeBars.children[10].title,"요청 없음");
  assert.equal(decodeBars.children[11].title,`${clockLabel(SLOT_START+900)}–${clockLabel(SLOT_START+1200)} · 요청 1개 · 12 tok/s`);
  assert.deepEqual(captions("decode"),["1시간 전","5분 단위","지금"],"an invalid bucket size falls back to five minutes");
  const stale={start:SLOT_START-300,requests:9,decode:999,prefill:999,ttft:999};
  render(withSeries({bucket_seconds:300,buckets:[stale,stale,...normalSeries.buckets]}));
  assert.deepEqual(visual().charts.decode,gaps({2:.5,5:1,11:.25}));
  assert.doesNotMatch(elements.get("decode-chart").attributes["aria-label"],/999/);
  render(withSeries({bucket_seconds:60,buckets:normalSeries.buckets}));
  assert.deepEqual(captions("decode"),["12분 전","1분 단위","지금"]);
  assert.equal(elements.get("decode-chart").title,"최근 12분 · 1분 단위");
  assert.equal(decodeBars.children[2].title,`${clockLabel(SLOT_START+600)}–${clockLabel(SLOT_START+660)} · 요청 3개 · 20 tok/s`);
  // Server restart required: the running server predates the time series, or its payload is unusable.
  const {request_series:omitted,...oldMonitor}=monitored.monitor;
  for(const old of [{...monitored,monitor:oldMonitor},withSeries({bucket_seconds:300}),{...monitored,monitor:undefined}]){
    render(old);
    assert.deepEqual(visual().charts.decode,gaps({}));
    assert.equal(elements.get("decode-chart").dataset.empty,"재시작 필요");
    assert.match(elements.get("decode-chart").attributes["aria-label"],/서버를 재시작하면 시간대별 차트/);
    assert.deepEqual(captions("decode"),["서버 재시작 필요"]);
    assert.equal(decodeBars.children[2].title,"");
  }
  // A stopped engine has no run to chart, even if a stale series is still in the payload.
  const stoppedEngine={...monitored.engine,running:false,ready:false};
  for(const idle of [{...withSeries(null),engine:stoppedEngine},{...monitored,engine:stoppedEngine}]){
    render(idle);
    assert.equal(visual().state,"stopped");
    assert.deepEqual(visual().charts.prefill,gaps({}));
    assert.equal(elements.get("prefill-chart").dataset.empty,"중지됨");
    assert.match(elements.get("prefill-chart").attributes["aria-label"],/엔진 중지됨/);
    assert.deepEqual(captions("prefill"),["엔진 중지됨"]);
    assert.equal(elements.get("prefill-bars").children[5].title,"");
  }
  render(monitored);
  assert.deepEqual(visual().charts.decode,gaps({2:.5,5:1,11:.25}),"a restarted engine draws its series again");
  assert.equal(elements.get("decode-chart").dataset.empty,"");
  render({...monitored,job:{state:"running",action:"/api/tune",message:"측정"}});
  assert.equal(visual().state,"tuning");
  render(monitored);
  assert.match(allText(elements.get("prefill")),/160.*tok\/s/);
  assert.match(allText(elements.get("memory")),/24.0.*GiB/);
  assert.match(text("request-label"),/이번 실행.*32토큰/);
  assert.match(text("monitor-error"),/메모리 할당 오류/);
  assert.match(allText(elements.get("request-history")),/취소 또는 실패.*완료.*100 \/ 32.*20/);
  assert.doesNotMatch(allText(elements.get("request-history")),/NaN|undefined/);
  render({...monitored,monitor:{...monitored.monitor,engine_started_at:200,memory:{state:"stopped",rss_bytes:null},errors:[]}});
  assert.match(text("request-label"),/이전 실행 포함/);
  assert.match(allText(elements.get("memory")),/—/);
  assert.match(text("monitor-error"),/최근 요청 미완료/);
  render({...monitored,engine:{...monitored.engine,running:false,ready:false,exit_code:-9}});
  assert.match(text("engine-state"),/중지됨/);
  assert.match(text("monitor-error"),/엔진이 종료되었습니다.*-9/);
  assert.equal(visual().state,"error");assert.deepEqual(visual().charts.decode,gaps({}));
  assert.equal(elements.get("memory-meter").hidden,true);
  render({...monitored,metrics:[],monitor:undefined});
  assert.match(allText(elements.get("request-history")),/아직 요청 기록이 없습니다/);
  assert.match(text("memory-detail"),/웹 서버를 재시작/);
  sandbox.fetch=async()=>{throw Error("disconnected fixture");};
  vm.runInContext("awaitRefresh=refresh()",sandbox);await sandbox.awaitRefresh;
  assert.equal(elements.get("start").disabled,true);
  assert.equal(elements.get("stop").disabled,true);
  assert.deepEqual({...visual()},{state:"offline"});
  assert.equal(elements.get("monitor").dataset.chartState,"offline");
  assert.equal(elements.get("memory-meter").hidden,true);
  const before=dispatched.length;window.dispatchEvent(new CustomEvent("flint:visuals-ready"));
  assert.deepEqual({...dispatched.slice(before).find(e=>e.type==="flint:monitor").detail},{state:"offline"});
  // Without the React bundle, tabs switch directly; with it, use-funnel receives the request.
  document.getElementById("workspace-content").scrollTop=300;
  vm.runInContext('tab("activity")',sandbox);
  assert.equal(text("breadcrumb"),"워크스페이스 / 상태 & 로그");
  assert.equal(elements.get("workspace-content").scrollTop,0);
  document.documentElement.dataset.navigation="funnel";
  vm.runInContext('tab("tuning")',sandbox);
  assert.equal(dispatched.at(-1).type,"flint:navigate");assert.equal(dispatched.at(-1).detail,"tuning");
  assert.equal(text("breadcrumb"),"워크스페이스 / 상태 & 로그");
  window.dispatchEvent(new CustomEvent("flint:screen",{detail:{name:"tuning",scroll:120}}));
  assert.equal(text("breadcrumb"),"워크스페이스 / 성능 튜닝");
  assert.equal(elements.get("workspace-content").scrollTop,120);
  window.dispatchEvent(new CustomEvent("flint:screen",{detail:{name:"../bogus",scroll:5}}));
  assert.equal(text("breadcrumb"),"워크스페이스 / 성능 튜닝");
  delete document.documentElement.dataset.navigation;
  assert.match(text("connection"),/마지막 조회값/);
  const calls=[];
  sandbox.fetch=async(url,options)=>{
    calls.push({url,body:options?.body});
    return {ok:true,status:200,json:async()=>url==="/api/state"?pending:{ok:true}};
  };
  render(verified);
  const tune=elements.get("tune").onclick();
  assert.match(text("confirm-text"),/기준 응답을 3회 검증.*2회 추가 재측정.*다른 속도가 3% 넘게 하락하지/);
  elements.get("confirm-dialog").returnValue="confirm";
  elements.get("confirm-dialog").listeners.close();
  await tune;
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(JSON.parse(calls.find(c=>c.url==="/api/tune").body),{});
  calls.length=0;
  render(clients);
  const connect=clientButton(0,"기존 CLI에 Flint 추가").onclick();
  assert.match(text("confirm-text"),/\/example\/pi\/models.json/);
  assert.match(text("confirm-text"),/다른 모델·기본 모델·로그인·프로젝트 설정은 변경하지 않습니다/);
  elements.get("confirm-dialog").returnValue="confirm";
  elements.get("confirm-dialog").listeners.close();
  await connect;
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(JSON.parse(calls.find(c=>c.url==="/api/connection").body),{name:"pi",enable:true});
  calls.length=0;
  render(connectedClients);
  const disconnect=clientButton(0,"Flint 연결 해제").onclick();
  elements.get("confirm-dialog").returnValue="confirm";
  elements.get("confirm-dialog").listeners.close();
  await disconnect;
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(JSON.parse(calls.find(c=>c.url==="/api/connection").body),{name:"pi",enable:false});
  for(const [fixture,index,label,url,body] of [
    [clients,2,"전용 실행기 생성","/api/integration",{name:"claude",enable:true}],
    [clients,3,"전용 실행기 생성","/api/integration",{name:"codex",enable:true}],
    [preparedClients,2,"claude-flint 명령 설치","/api/alias",{name:"claude",enable:true}],
    [preparedClients,3,"전용 명령 제거","/api/alias",{name:"codex",enable:false}],
    [preparedClients,2,"실행기 복원 / 제거","/api/integration",{name:"claude",enable:false}],
    [legacyClients,0,"전용 명령 제거","/api/alias",{name:"pi",enable:false}],
  ]){
    calls.length=0;render(fixture);
    const change=clientButton(index,label).onclick();
    elements.get("confirm-dialog").returnValue="confirm";
    elements.get("confirm-dialog").listeners.close();
    await change;
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(JSON.parse(calls.find(c=>c.url===url).body),body);
  }
  calls.length=0;
  render(progress);
  elements.get("tuning-cancel").onclick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls[0].url,"/api/cancel");
  calls.length=0;
  render(pending);
  const apply=elements.get("tuning-apply").onclick();
  elements.get("tuning-apply").onclick(); // Double click is suppressed.
  await apply;
  const requests=calls.filter(c=>c.url==="/api/tune/apply");
  assert.equal(requests.length,1);
  assert.deepEqual(JSON.parse(requests[0].body),{tuning_id:"fixture"});
  for(const reason of ["측정 후 설정이 변경되었습니다. 다시 튜닝하세요.",
    "측정 환경이 달라졌습니다. 다시 튜닝하세요.",
    "여유 디스크 기준 캐시 용량이 줄었습니다. 다시 튜닝하세요.",
    "적용 가능한 대기 제안이 없거나 결과 ID가 다릅니다."]){
    sandbox.fetch=async(url)=>({ok:url!=="/api/tune/apply",status:url==="/api/tune/apply"?409:200,
      json:async()=>url==="/api/state"?pending:{error:reason}});
    elements.get("tune-view").onclick();
    await elements.get("tuning-apply").onclick();
    assert.equal(text("tuning-request-error"),reason);
    assert.equal(elements.get("tuning-request-error").classList.contains("hidden"),false);
    assert.equal(elements.get("tuning-dialog").open,true);
    assert.equal(elements.get("tune-pending").classList.contains("hidden"),false);
    assert.equal(elements.get("tuning-apply").classList.contains("hidden"),false);
    assert.doesNotMatch(text("notice"),/제안을 적용하고 서버를 시작했습니다/);
    vm.runInContext("awaitRefresh=refresh()",sandbox);
    await sandbox.awaitRefresh;
    assert.equal(text("tuning-request-error"),reason); // Successful poll must not clear refusal.
    elements.get("tuning-close").onclick();
    elements.get("tune-view").onclick();
    assert.equal(text("tuning-request-error"),reason);
  }
  sandbox.fetch=async()=>{throw Error("fixture network unavailable");};
  await elements.get("tuning-apply").onclick();
  assert.match(text("tuning-request-error"),/fixture network unavailable/);
  sandbox.fetch=async(url)=>({ok:true,status:200,json:async()=>url==="/api/state"?pending:{ok:true}});
  vm.runInContext("awaitRefresh=refresh()",sandbox);await sandbox.awaitRefresh;
  assert.match(text("tuning-request-error"),/fixture network unavailable/);
  let release;
  sandbox.fetch=url=>url==="/api/tune/apply"?new Promise(resolve=>{release=resolve;}):
    Promise.resolve({ok:true,status:200,json:async()=>pending});
  const retry=elements.get("tuning-apply").onclick();
  assert.equal(text("tuning-request-error"),""); // Retry starts by clearing the old failure.
  release({ok:false,status:409,json:async()=>({error:"retry refused"})});
  await retry;
  assert.equal(text("tuning-request-error"),"retry refused");
  render({...pending,tuning:{...pending.tuning,tuning_id:"new-run"}});
  assert.equal(text("tuning-request-error"),""); // Only a new run or retry clears it.
  const historyId="00000000-0000-4000-8000-000000000001";
  const historyState={...pending,capabilities:{...pending.capabilities,tuning_history:true},tuning_history:[
    {tuning_id:historyId,context:32768,environment:{chip:"Apple M4 Pro",gpu_cores:20,memory_gib:48,macos:"27.0"},
      finished_at:1793330000,state:"done"}]};
  const historyReport={...pending.tuning,tuning_id:historyId,context:32768,
    environment:{chip:"Apple M4 Pro",gpu_cores:20,memory_gib:48,macos:"27.0"}};
  calls.length=0;
  sandbox.fetch=async(url,options)=>{
    calls.push({url,body:options?.body});
    return {ok:true,status:200,json:async()=>url.startsWith("/api/tune/history/")?historyReport:historyState};
  };
  render(historyState);
  assert.match(allText(elements.get("tuning-history-select")),/문맥 32,768/);
  vm.runInContext('tuningRequestError="preserve current refusal";renderTuningError()',sandbox);
  await elements.get("tuning-history-open").onclick();
  assert.equal(calls[0].url,"/api/tune/history/"+historyId);
  assert.match(text("tuning-title"),/보관된 튜닝 기록.*읽기 전용/);
  assert.match(text("tuning-environment"),/설정 문맥 32,768/);
  assert.equal(elements.get("tuning-apply").classList.contains("hidden"),true);
  assert.equal(elements.get("tuning-cancel").classList.contains("hidden"),true);
  assert.equal(text("tuning-request-error"),"");
  await elements.get("tuning-apply").onclick();
  assert.equal(calls.filter(c=>c.url==="/api/tune/apply").length,0);
  elements.get("tuning-close").onclick();
  elements.get("tune-view").onclick();
  assert.equal(elements.get("tuning-apply").classList.contains("hidden"),false);
  assert.equal(text("tuning-request-error"),"preserve current refusal");
  assert.doesNotMatch(text("tuning-title"),/보관된/);
  console.log("Tuning modal smoke passed: progress/result, stage metrics, comparison, pending, close/Esc/later/apply/cancel, errors, legacy capability");
}
async function chartIntervalSmoke(){
  const select=elements.get("chart-interval"), key="flint.chartSeconds";
  const names=["decode","prefill","ttft"], calls=[];
  const series=seconds=>({...normalSeries,bucket_seconds:seconds,buckets:normalSeries.buckets.map((b,i)=>({
    ...b,start:SLOT_START+i*seconds}))});
  const payload=seconds=>({...current,capabilities:{chart_intervals:true,chart_interval_seconds:[5,60,300]},monitor:{
    engine_started_at:100,memory:{state:"measured",rss_bytes:1024},errors:[],request_series:series(seconds)}});
  const reply=data=>({ok:true,status:200,json:async()=>data});
  const captions=name=>elements.get(`${name}-chart-label`).children.map(e=>e.textContent);
  const poll=()=>vm.runInContext("refresh()",sandbox);
  const choose=seconds=>{select.value=String(seconds);return select.onchange();};
  sandbox.fetch=async(url,options)=>{
    calls.push({url,options});
    return reply(payload(Number(new URL(url,"http://localhost").searchParams.get("chart_seconds")||300)));
  };
  render(payload(300));
  assert.equal(select.disabled,false);
  assert.equal(select.value,"300");
  const slots=[...elements.get("decode-bars").children];
  for(const [seconds,range,step] of [[60,"12분","1분"],[5,"1분","5초"],[300,"1시간","5분"]]){
    await choose(seconds);
    assert.equal(storage.get(key),String(seconds));
    assert.equal(vm.runInContext("readChartSeconds()",sandbox),seconds,"choice survives a new page's preference read");
    for(const name of names){
      assert.deepEqual(captions(name),[`${range} 전`,`${step} 단위`,"지금"]);
      assert.equal(elements.get(`${name}-chart`).attributes["aria-busy"],"false");
      assert.match(elements.get(`${name}-chart`).title,new RegExp(`최근 ${range} · ${step} 단위`));
      assert.ok(elements.get(`${name}-bars`).children[2].title.startsWith(
        `${clockLabel(SLOT_START+2*seconds,seconds<60)}–${clockLabel(SLOT_START+3*seconds,seconds<60)}`));
      if(seconds===5){
        assert.match(elements.get(`${name}-bars`).children[2].title,/^\d{2}:\d{2}:10–\d{2}:\d{2}:15/);
        assert.ok(elements.get(`${name}-bars`).children[11].title.startsWith(
          `${clockLabel(SLOT_START+55,true)}–${clockLabel(SLOT_START+60,true)}`),"seconds cross a minute boundary correctly");
        assert.equal(new Set(elements.get(`${name}-bars`).children.map(slot=>slot.title.split(" · ")[0])).size,12);
        assert.ok(elements.get(`${name}-chart`).attributes["aria-label"].includes(clockLabel(SLOT_START+10,true)));
      }
    }
    assert.ok(elements.get("decode-bars").children.every((node,i)=>node===slots[i]));
    assert.equal(elements.get("memory-meter").value,1024,"time selection does not change the memory gauge");
    assert.equal(allText(elements.get("decode")).trim(),"—  tok/s","headline still uses the most recent completed request");
  }
  assert.deepEqual(calls.map(c=>c.url),["/api/state?chart_seconds=60","/api/state?chart_seconds=5","/api/state"]);
  assert.ok(calls.every(c=>c.options.method==="GET"&&c.options.body===undefined));
  // A poll already in flight must not render a response for the previous interval.
  let release;
  sandbox.fetch=url=>{calls.push({url});return new Promise(resolve=>{release=resolve;});};
  const first=poll();
  await choose(60);
  assert.equal(elements.get("decode-chart").attributes["aria-busy"],"true");
  release(reply(payload(300)));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.at(-1).url,"/api/state?chart_seconds=60");
  await choose(5);
  release(reply(payload(60)));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.at(-1).url,"/api/state?chart_seconds=5");
  assert.deepEqual(captions("decode"),["1시간 전","5분 단위","지금"],"stale responses do not repaint the charts");
  release(reply(payload(5)));
  await first;
  assert.deepEqual(captions("decode"),["1분 전","5초 단위","지금"]);
  // A failed switch leaves the actual old axis visible and is retried by the next poll.
  sandbox.fetch=async()=>{throw Error("interval offline");};
  await choose(60);
  assert.match(text("connection"),/interval offline.*마지막 조회값/);
  assert.deepEqual(captions("decode"),["1분 전","5초 단위","지금"]);
  assert.equal(elements.get("decode-chart").attributes["aria-busy"],"false");
  sandbox.fetch=async()=>reply(payload(60));
  await poll();
  assert.deepEqual(captions("decode"),["12분 전","1분 단위","지금"]);
  assert.equal(elements.get("connection").classes.has("hidden"),true);
  sandbox.fetch=async()=>reply(payload(5));
  await choose(5);
  // A boolean from the previous release cannot authorize the new five-second query.
  for(const capabilities of [{}, {chart_intervals:true},
    {chart_intervals:true,chart_interval_seconds:[60,300,900]},
    {chart_intervals:true,chart_interval_seconds:"5,60,300"}]){
    render({...payload(300),capabilities});
    assert.equal(select.disabled,true);
    assert.equal(select.value,"300");
    assert.match(select.title,/웹 서버를 재시작/);
    assert.equal(storage.get(key),"5","old servers do not overwrite a stored preference");
    calls.length=0;
    sandbox.fetch=async url=>{calls.push(url);return reply({...payload(300),capabilities});};
    await poll();
    assert.deepEqual(calls,["/api/state"]);
  }
  // Discover a restarted server, then fetch the preferred unit before rendering.
  calls.length=0;
  sandbox.fetch=async url=>{calls.push(url);return reply(payload(url==="/api/state"?300:5));};
  await poll();
  assert.deepEqual(calls,["/api/state","/api/state?chart_seconds=5"]);
  assert.equal(select.disabled,false);
  assert.equal(select.value,"5");
  assert.deepEqual(captions("decode"),["1분 전","5초 단위","지금"]);
  for(const special of [
    {...payload(60),engine:{...current.engine,running:false,ready:false},monitor:{request_series:null}},
    {...payload(60),job:{state:"running",action:"/api/tune",message:"측정"}},
  ]){
    render(special);assert.equal(select.disabled,false,"stopped engines and tuning do not lock the view preference");
  }
  const localStorage=window.localStorage;
  window.localStorage={getItem(){throw Object.assign(Error("denied"),{name:"SecurityError"});},
    setItem(){throw Object.assign(Error("full"),{name:"QuotaExceededError"});}};
  assert.equal(vm.runInContext("readChartSeconds()",sandbox),300);
  assert.match(text("notice"),/읽을 수 없어 기본 5분/);
  sandbox.fetch=async()=>reply(payload(5));
  await choose(5);
  assert.match(text("notice"),/적용하지만 브라우저에 저장하지 못했습니다/);
  assert.deepEqual(captions("decode"),["1분 전","5초 단위","지금"]);
  window.localStorage=localStorage;
  storage.set(key,"900");
  assert.equal(vm.runInContext("readChartSeconds()",sandbox),300);
  assert.equal(storage.get(key),"300");
  assert.match(text("notice"),/15분 단위가 제거되어 기본 5분 단위/);
  calls.length=0;
  await choose(900);
  assert.match(text("notice"),/5초, 1분, 5분/);
  assert.equal(calls.length,0,"the removed interval is never sent");
  for(const invalid of ["", "0", "60.0", "3600", "null"]){
    storage.set(key,invalid);
    assert.equal(vm.runInContext("readChartSeconds()",sandbox),300);
    assert.match(text("notice"),/올바르지 않아 기본 5분/);
  }
  storage.delete(key);
  assert.equal(vm.runInContext("readChartSeconds()",sandbox),300);
  console.log("Chart interval smoke passed: common selector, persistence, races, GET-only, old servers, offline, storage failures");
}
actionsSmoke().then(chartIntervalSmoke).catch(error=>{console.error(error);process.exitCode=1;});
