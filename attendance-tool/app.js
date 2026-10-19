const BUILTIN_CLASS3 = ["أسامة ناصر بن جزاء الحربي", "ابراهيم محمد ابراهيم ال عتيق", "الوليد حسن حمود القحطاني", "الياس تركي بن صالح بن مخرب", "بسام سلمان محمد القرني", "بندر بن غالب بن حميد بن حمود الرحيمي المطيري", "تمام محمد فنيس القحطاني", "حسام محمد بن عبيد الحربي", "حمدان محمد مهيوب حسن", "راكان مهدي محماس المطيري", "زايد سعد عجير العتيبي", "سلطان علي مبارك القحطاني", "عبدالرحمن محمد مطهر الجرادي", "عبدالعزيز عبدالرحمن ابن سعيد العبيدي", "عبدالله سامي عبدالله العلياني", "عدي مهدي محيسن المطيري", "عز منصور مسفر القحطاني", "علي عبدالرحيم محمد العجمي", "فارس بن فهد بن أحمد المحاثلي", "ماجد أحمد عوض المطيري", "ماجد مسلط بن عساف العتيبي", "محمد عبدالله بن احمد بن ظافر", "نادر عبدالله طالع عسيري", "ناصر ماجد محمد القبلان", "نايف بن حنيتم بن مسيمير العصيمي العتيبي", "نايف محمد حمود ضاري", "وليد عيد زايد العتيبي", "وليد نايف فلاح العتيبي", "يزيد سعد مذكر العتيبي", "يزيد سعود بن هاجد الحربي", "يزيد عبدالله محمد العنزي", "يزيد عيد زايد العتيبي"];

const $ = id => document.getElementById(id);
let latestStudents = [];

function normalizeArabic(s){
  return (s||"")
    .toLowerCase()
    .replace(/[\u064B-\u065F\u0670\u06D6-\u06ED]/g,"")
    .replace(/ـ/g,"")
    .replace(/[أإآٱ]/g,"ا")
    .replace(/ى/g,"ي")
    .replace(/ؤ/g,"و")
    .replace(/ئ/g,"ي")
    .replace(/ة/g,"ه")
    .replace(/[\u200f\u200e]/g,"")
    .replace(/[^\u0600-\u06ff0-9a-z ]/gi," ")
    .replace(/\s+/g," ")
    .trim();
}
const STOP = new Set(["بن","ابن","بنت","ال"]);
function tokens(s){
  return normalizeArabic(s).split(" ").filter(x=>x && !STOP.has(x));
}
function levenshtein(a,b){
  a=normalizeArabic(a); b=normalizeArabic(b);
  const m=a.length,n=b.length;
  if(!m) return n; if(!n) return m;
  const prev=Array.from({length:n+1},(_,i)=>i), cur=new Array(n+1);
  for(let i=1;i<=m;i++){
    cur[0]=i;
    for(let j=1;j<=n;j++){
      cur[j]=Math.min(cur[j-1]+1,prev[j]+1,prev[j-1]+(a[i-1]===b[j-1]?0:1));
    }
    for(let j=0;j<=n;j++) prev[j]=cur[j];
  }
  return prev[n];
}
function sim(a,b){
  const na=normalizeArabic(a), nb=normalizeArabic(b);
  if(na===nb) return 1;
  const ta=tokens(a), tb=tokens(b);
  const setb=new Set(tb);
  const common=ta.filter(x=>setb.has(x)).length;
  const coverage = ta.length ? common/Math.min(ta.length,tb.length||1) : 0;
  const contains = (ta.length>=2 && ta.every(x=>setb.has(x))) || (tb.length>=2 && tb.every(x=>new Set(ta).has(x)));
  const char = 1 - levenshtein(na,nb)/Math.max(na.length,nb.length,1);
  return Math.max(contains?0.94:0, coverage*0.82+char*0.18);
}
function bestMatch(name, roster, used){
  let arr=roster.map((r,i)=>({i,r,s:used.has(i)?-1:sim(name,r)})).sort((a,b)=>b.s-a.s);
  const best=arr[0], second=arr[1]||{s:0};
  if(!best || best.s<0.58) return null;
  if(best.s<0.90 && (best.s-second.s)<0.08) return null;
  return best;
}
function decodeArrayBuffer(buf){
  const u8=new Uint8Array(buf);
  if(u8[0]===0xFF && u8[1]===0xFE) return new TextDecoder("utf-16le").decode(buf);
  if(u8[0]===0xFE && u8[1]===0xFF) return new TextDecoder("utf-16be").decode(buf);
  return new TextDecoder("utf-8").decode(buf);
}
function parseDuration(s){
  s=(s||"").trim();
  let sec=0;
  const h=s.match(/(\d+)\s*س/); if(h) sec += +h[1]*3600;
  const m=s.match(/(\d+)\s*د/); if(m) sec += +m[1]*60;
  const ss=s.match(/(\d+)\s*ث/); if(ss) sec += +ss[1];
  if(!sec && /^\d+:\d+/.test(s)){
    const p=s.split(":").map(Number); if(p.length===2) sec=p[0]*60+p[1]; else if(p.length===3) sec=p[0]*3600+p[1]*60+p[2];
  }
  return sec;
}
function formatDuration(sec){
  sec=Math.max(0,Math.round(sec||0));
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;
  if(h) return `${h} س ${m} د`;
  if(m) return s?`${m} د ${s} ث`:`${m} دقيقة`;
  return `${s} ثانية`;
}
function parseDate(v){
  if(!v) return null;
  const d=new Date(v);
  return isNaN(d)?null:d;
}
function timeText(d){
  if(!d) return "—";
  return d.toLocaleTimeString("ar-SA",{hour:"numeric",minute:"2-digit"});
}
function dateText(d){
  if(!d) return "—";
  return d.toLocaleDateString("ar-SA",{year:"numeric",month:"2-digit",day:"2-digit"});
}
function splitTSV(line){return line.split("\t");}

function parseTeamsText(text, fileTag){
  const lines=text.replace(/\r/g,"").split("\n");
  const result={title:"",start:null,end:null,duration:0,participants:[]};
  for(const ln of lines){
    const c=splitTSV(ln);
    if(c[0]==="عنوان الاجتماع") result.title=c[1]||"";
    if(c[0]==="وقت البدء") result.start=parseDate(c[1]);
    if(c[0]==="وقت الانتهاء") result.end=parseDate(c[1]);
    if(c[0]==="مدة الاجتماع") result.duration=parseDuration(c[1]);
  }
  let startIdx=lines.findIndex(x=>x.trim()==="2. المشاركون");
  if(startIdx<0) startIdx=lines.findIndex(x=>x.includes("المشاركون") && x.trim().startsWith("2"));
  if(startIdx>=0){
    let h=-1;
    for(let i=startIdx+1;i<Math.min(lines.length,startIdx+10);i++){
      if(lines[i].startsWith("الاسم\t")){h=i;break;}
    }
    if(h>=0){
      const headers=splitTSV(lines[h]);
      const ix = name => headers.indexOf(name);
      for(let i=h+1;i<lines.length;i++){
        const line=lines[i];
        if(!line.trim()) break;
        if(line.trim().startsWith("3. الأنشطة")) break;
        const c=splitTSV(line);
        const name=(c[ix("الاسم")]||"").trim();
        if(!name) continue;
        const role=(c[ix("الدور")]||"").trim();
        result.participants.push({
          name,
          join:parseDate(c[ix("وقت الانضمام الأول")]),
          leave:parseDate(c[ix("وقت المغادرة الأخير")]),
          seconds:parseDuration(c[ix("مدة الاجتماع")]),
          role,
          sig:[fileTag,name,c[ix("وقت الانضمام الأول")]||"",c[ix("وقت المغادرة الأخير")]||"",c[ix("مدة الاجتماع")]||""].join("|")
        });
      }
    }
  }
  return result;
}
function getRoster(){
  const sel=$("rosterSelect").value;
  if(sel==="builtin3") return BUILTIN_CLASS3.slice();
  const lines=$("customRoster").value.split(/\n/).map(x=>x.trim()).filter(Boolean);
  return [...new Set(lines)];
}
function loadSavedRosters(){
  const saved=JSON.parse(localStorage.getItem("attendance_rosters")||"{}");
  const sel=$("rosterSelect");
  [...sel.querySelectorAll("option[data-saved]")].forEach(o=>o.remove());
  Object.keys(saved).forEach(name=>{
    const o=document.createElement("option"); o.value="saved:"+name; o.textContent=name+" (محفوظ)"; o.dataset.saved="1"; sel.appendChild(o);
  });
}
function applyRosterSelection(){
  const v=$("rosterSelect").value;
  if(v.startsWith("saved:")){
    const name=v.slice(6), saved=JSON.parse(localStorage.getItem("attendance_rosters")||"{}");
    $("customRosterName").value=name;
    $("customRoster").value=(saved[name]||[]).join("\n");
    $("rosterSelect").value="custom";
  }
}
function toggleCustomFields(){
  const isCustom=$("rosterSelect").value==="custom";
  document.querySelectorAll(".custom-only").forEach(el=>el.classList.toggle("hidden",!isCustom));
}
$("rosterSelect").addEventListener("change",()=>{applyRosterSelection();toggleCustomFields();});
$("saveRosterBtn").addEventListener("click",()=>{
  const name=$("customRosterName").value.trim();
  const roster=$("customRoster").value.split(/\n/).map(x=>x.trim()).filter(Boolean);
  if(!name || !roster.length){alert("اكتب اسم الكشف وأسماء الطلاب أولًا.");return;}
  const saved=JSON.parse(localStorage.getItem("attendance_rosters")||"{}");
  saved[name]=roster; localStorage.setItem("attendance_rosters",JSON.stringify(saved));
  loadSavedRosters();
  toggleCustomFields(); alert("تم حفظ الكشف على هذا الجهاز.");
});
$("clearBtn").addEventListener("click",()=>{
  $("teamsFiles").value="";
  $("reviewBox").classList.remove("show");
});
$("printBtn").addEventListener("click",()=>window.print());

async function generate(){
  const roster=getRoster();
  if(!roster.length){alert("لا يوجد كشف طلاب.");return;}
  const files=[...$("teamsFiles").files];
  if(!files.length){alert("ارفع تقرير Teams أولًا.");return;}

  const parsed=[];
  for(const f of files){
    const text=decodeArrayBuffer(await f.arrayBuffer());
    parsed.push(parseTeamsText(text,f.name));
  }

  let start=null,end=null,title="",meetingDur=0;
  for(const r of parsed){
    if(r.start && (!start || r.start<start)) start=r.start;
    if(r.end && (!end || r.end>end)) end=r.end;
    if(!title && r.title) title=r.title;
    meetingDur=Math.max(meetingDur,r.duration||0);
  }
  if(start && end) meetingDur=Math.round((end-start)/1000);

  const raw=[];
  parsed.forEach(r=>r.participants.forEach(p=>raw.push(p)));
  const seenSig=new Set();
  const participants=raw.filter(p=>{
    if(seenSig.has(p.sig)) return false;
    seenSig.add(p.sig); return true;
  });

  const grouped=new Map();
  for(const p of participants){
    if(normalizeArabic(p.role)==normalizeArabic("المنظم")) continue;
    const key=normalizeArabic(p.name);
    if(!grouped.has(key)) grouped.set(key,{name:p.name,join:p.join,leave:p.leave,seconds:0,segments:new Set()});
    const g=grouped.get(key);
    const seg=(p.join?p.join.getTime():"")+"|"+(p.leave?p.leave.getTime():"")+"|"+p.seconds;
    if(!g.segments.has(seg)){g.seconds+=p.seconds||0;g.segments.add(seg);}
    if(p.join && (!g.join || p.join<g.join)) g.join=p.join;
    if(p.leave && (!g.leave || p.leave>g.leave)) g.leave=p.leave;
  }

  const used=new Set();
  const matchByRoster=new Map();
  const ignored=[];
  const review=[];

  for(const g of grouped.values()){
    const m=bestMatch(g.name,roster,used);
    if(m){
      used.add(m.i); matchByRoster.set(m.i,g);
      if(normalizeArabic(g.name)!==normalizeArabic(m.r)) review.push(`${g.name} ← ${m.r}`);
    }else{
      ignored.push(g.name);
    }
  }

  latestStudents=roster.map((name,i)=>{
    const g=matchByRoster.get(i);
    return {name,present:!!g,join:g?.join||null,seconds:g?.seconds||0};
  });

  const present=latestStudents.filter(x=>x.present).length;
  const absent=latestStudents.length-present;
  const rate=latestStudents.length?present/latestStudents.length*100:0;

  $("rSchool").textContent=$("school").value.trim()||"—";
  $("rTeacher").textContent=$("teacher").value.trim()||"—";
  $("fTeacher").textContent=$("teacher").value.trim()||"—";
  $("fPrincipal").textContent=$("principal").value.trim()||"—";
  $("rSubject").textContent=$("subject").value.trim()||"—";
  $("rGrade").textContent=$("grade").value.trim()||"—";
  $("rSection").textContent=$("section").value.trim()||"—";
  $("rMeetingTitle").textContent=title?`Microsoft Teams — ${title}`:"Microsoft Teams";
  $("rDate").textContent=dateText(start||new Date());
  $("rMeetingDuration").textContent=formatDuration(meetingDur);
  $("kTotal").textContent=latestStudents.length;
  $("kPresent").textContent=present;
  $("kAbsent").textContent=absent;
  $("kRate").textContent=rate.toFixed(1)+"%";
  $("kStart").textContent=timeText(start);
  $("kEnd").textContent=timeText(end);
  $("bigPresent").textContent=present;
  $("bigAbsent").textContent=absent;
  $("donutRate").textContent=rate.toFixed(0)+"%";
  $("legendPresent").textContent=present;
  $("legendAbsent").textContent=absent;
  $("donut").style.setProperty("--deg",(rate*3.6).toFixed(1)+"deg");

  const tb=$("reportRows"); tb.innerHTML="";
  latestStudents.forEach((s,i)=>{
    const tr=document.createElement("tr");
    tr.innerHTML=`
      <td>${i+1}</td>
      <td class="name" title="${s.name.replace(/"/g,'&quot;')}">${s.name}</td>
      <td>${s.present?timeText(s.join):"—"}</td>
      <td>${s.present?formatDuration(s.seconds):"0 دقيقة"}</td>
      <td><span class="badge ${s.present?"present":"absent"}">${s.present?"حاضر":"غائب"}</span></td>`;
    tb.appendChild(tr);
  });

  const list=$("reviewList"); list.innerHTML="";
  const li1=document.createElement("li");
  li1.textContent=`تمت مطابقة ${present} طالب من أصل ${roster.length}.`;
  list.appendChild(li1);
  if(review.length){
    const li=document.createElement("li");
    li.textContent="مطابقات أسماء مختصرة: "+review.join("، ");
    list.appendChild(li);
  }
  if(ignored.length){
    const li=document.createElement("li");
    li.textContent="تم تجاهل أسماء غير موجودة في كشف الفصل: "+ignored.join("، ");
    list.appendChild(li);
  }
  $("reviewBox").classList.add("show");
  document.getElementById("report").scrollIntoView({behavior:"smooth",block:"start"});
}
$("generateBtn").addEventListener("click",generate);
$("generateBtnTop").addEventListener("click",generate);
$("generateBtnSticky").addEventListener("click",generate);
$("printBtnSticky").addEventListener("click",()=>window.print());

(function init(){
  loadSavedRosters();
  const roster=BUILTIN_CLASS3;
  $("kTotal").textContent=roster.length;
  $("kAbsent").textContent=roster.length;
  $("bigAbsent").textContent=roster.length;
  $("legendAbsent").textContent=roster.length;
  const tb=$("reportRows");
  roster.forEach((name,i)=>{
    const tr=document.createElement("tr");
    tr.innerHTML=`<td>${i+1}</td><td class="name">${name}</td><td>—</td><td>0 دقيقة</td><td><span class="badge absent">غائب</span></td>`;
    tb.appendChild(tr);
  });
})();