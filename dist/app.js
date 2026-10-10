'use strict';
const $ = (id) => document.getElementById(id);
const plannedCameras = () => Array.from({length:33}, (_,i) => ({
  id:'bodycam'+String(i+1).padStart(2,'0'),name:'BODY CAM '+String(i+1).padStart(2,'0'),
  group:'',officer:'',model:'',mode:'publish',status:'unknown',enabled:true,
  recording_enabled:true,source_configured:false
}));
const hosted = location.hostname.endsWith('.chatgpt.site');
const state = {
  base:localStorage.getItem('bodycam_api') || (hosted ? '' : location.origin),
  token:sessionStorage.getItem('bodycam_token') || '',
  role:sessionStorage.getItem('bodycam_role') || '',cameras:plannedCameras(),
  connected:false,mediaOnline:false,filter:'all',query:'',selected:'bodycam01',
  cards:new Map(),players:new Map(),visible:new Set(),gridOrder:'',pending:null,
  recordingOffset:0,recordingTotal:0,refreshing:false
};
let toastTimer;
function toast(message) { $('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},5500); }
function notice(message) { $('notice').textContent=message;$('notice').hidden=!message; }
function element(tag,className,text) { const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node; }
function action(text,fn,className='secondary') { const b=element('button',className,text);b.type='button';b.addEventListener('click',fn);return b; }
function bytes(n) { if(!Number.isFinite(n))return '—';if(n>=1e9)return (n/1e9).toFixed(1)+' GB';if(n>=1e6)return (n/1e6).toFixed(1)+' MB';return (n/1e3).toFixed(0)+' KB'; }
function timestamp(n) { return n ? new Date(n*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'medium',timeStyle:'short'})+' IST' : 'Time unavailable'; }
function openDialog(id) { if(!$(id).open)$(id).showModal(); }
function requireLogin(callback) { if(!state.token || !state.base) {state.pending=callback;openLogin();return false;}return true; }
function openLogin() { $('server-url').value=state.base;$('password').value='';$('login-error').textContent='';openDialog('login-dialog'); }
function url(path) { return state.base.replace(/\/+$/,'')+path; }
async function api(path,options={}) {
  const {raw=false,...rest}=options;
  const headers={...(rest.headers||{})};
  if(state.token&&path!=='/api/auth/login')headers.Authorization='Bearer '+state.token;
  if(rest.body && typeof rest.body !== 'string') {headers['Content-Type']='application/json';rest.body=JSON.stringify(rest.body);}
  const response=await fetch(url(path),{...rest,headers});
  if(!response.ok) {
    let message='Request failed ('+response.status+')';
    try {const body=await response.json();message=Array.isArray(body.detail)?body.detail.map(x=>x.msg).join('; '):(body.detail||message);}catch{}
    const error=new Error(message);error.status=response.status;throw error;
  }
  return raw ? response : response.json();
}
const observer = new IntersectionObserver(entries=>{
  entries.forEach(entry=>{
    const id=entry.target.dataset.camera;
    if(entry.isIntersecting) {state.visible.add(id);ensurePlayer(id);}
    else if(document.fullscreenElement!==entry.target) {state.visible.delete(id);stopPlayer(id);}
  });
},{rootMargin:'100px'});
function makeCard(cam) {
  const card=element('article','camera-card');card.dataset.camera=cam.id;
  const heading=element('div','camera-heading'),left=element('div','camera-heading-left');
  const name=element('span','camera-name');left.append(name);
  const full=action('⤢ Full View',async()=>{
    try {
      if(card.requestFullscreen)await card.requestFullscreen();
      else {
        const p=state.players.get(cam.id);
        if(p?.video.webkitEnterFullscreen){p.video.controls=true;p.video.webkitEnterFullscreen();}
        else toast('Full-screen playback is not available in this browser.');
      }
    }catch{toast('Full-screen playback is not available in this browser.');}
  },'full-view');full.setAttribute('aria-label','Full view of '+cam.name);left.append(full);
  const group=element('div','camera-group'),groupText=element('span'),dot=element('span','dot unknown');
  group.append(groupText,dot);heading.append(left,group);
  const area=element('div','stream-area'),placeholder=element('div','stream-placeholder');
  const waiting=element('div'),detail=element('small'),dots=element('div','loading-dots');
  for(let i=0;i<3;i++)dots.append(element('span'));
  placeholder.append(waiting,detail,dots);area.append(placeholder);
  const footer=element('div','camera-footer'),link=element('span'),rec=element('span','record-indicator');
  const setup=action('Setup',()=>openSetup(cam.id),'text-button');footer.append(link,rec,setup);
  card.append(heading,area,footer);
  const parts={card,name,full,groupText,dot,area,placeholder,waiting,detail,link,rec,setup};
  state.cards.set(cam.id,parts);return parts;
}
function updateCard(cam) {
  const p=state.cards.get(cam.id);
  p.name.textContent=cam.name;p.full.setAttribute('aria-label','Full view of '+cam.name);
  p.groupText.textContent=cam.group||'Unassigned';p.dot.className='dot '+cam.status;p.dot.title=cam.status;
  p.link.textContent=cam.mode==='publish'?'SIM uplink':'RTSP · VPN / APN';
  p.setup.hidden=!!state.token&&state.role!=='admin';
  const live=cam.status==='online';
  p.rec.className='record-indicator'+(cam.recording?' live':'');
  p.rec.textContent=cam.recording?'● Recording':cam.recording_enabled?(live?'Recording pending':'Recording ready'):'Recording off';
  p.waiting.textContent=live?'Connecting to body camera…':cam.status==='disabled'?'Camera disabled':cam.status==='offline'?'Waiting for body camera stream…':'Waiting for central server connection…';
  p.detail.textContent=live?'Negotiating live playback':cam.status==='offline'?'Check the camera’s SIM and publishing settings.':cam.status==='unknown'?'Connect the server to receive live status.':'';
  if(!live)stopPlayer(cam.id);
  else if(state.visible.has(cam.id))ensurePlayer(cam.id);
}
function renderGrid() {
  $('manage-button').hidden=!!state.token&&state.role!=='admin';
  const cameras=state.cameras.filter(c=>{
    const filter=state.filter==='all'||(state.filter==='active'?c.status==='online':c.group.toLowerCase()===state.filter.toLowerCase());
    return filter&&(c.name+' '+c.group+' '+c.officer+' '+c.id).toLowerCase().includes(state.query.toLowerCase());
  });
  const ids=new Set(cameras.map(c=>c.id));
  for(const [id,p] of state.cards)if(!ids.has(id)){observer.unobserve(p.card);stopPlayer(id);state.visible.delete(id);p.card.remove();state.cards.delete(id);}
  const order=cameras.map(c=>c.id).join(',');
  if(order!==state.gridOrder) {
    for(const cam of cameras){const p=state.cards.get(cam.id)||makeCard(cam);$('camera-grid').append(p.card);observer.observe(p.card);}
    state.gridOrder=order;
  }
  cameras.forEach(updateCard);$('empty-grid').hidden=cameras.length>0;
  document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===state.filter));
}
function stopPlayer(id) {
  const p=state.players.get(id);if(!p)return;
  state.players.delete(id);p.closed=true;clearTimeout(p.fallbackTimer);clearTimeout(p.retryTimer);clearInterval(p.watchdog);
  p.controller.abort();if(p.hls)p.hls.destroy();if(p.pc)p.pc.close();
  if(p.session&&state.token)fetch(p.session,{method:'DELETE',headers:{Authorization:'Bearer '+state.token}}).catch(()=>{});
  p.video.pause();p.video.srcObject=null;p.video.removeAttribute('src');p.video.load();p.video.remove();
  const card=state.cards.get(id);if(card)card.placeholder.hidden=false;
}
function ensurePlayer(id) {
  const cam=state.cameras.find(c=>c.id===id);
  if(state.players.has(id)||!cam||cam.status!=='online'||!state.token||!state.connected)return;
  const card=state.cards.get(id);if(!card)return;
  const video=element('video');video.autoplay=true;video.muted=true;video.playsInline=true;
  video.setAttribute('aria-label','Live video: '+cam.name);card.area.append(video);
  const p={video,closed:false,controller:new AbortController(),pc:null,hls:null,session:null,lastFrame:Date.now(),hlsMode:false};
  state.players.set(id,p);
  video.addEventListener('playing',()=>{card.placeholder.hidden=true;p.lastFrame=Date.now();});
  if(video.requestVideoFrameCallback) {
    const frame=()=>{if(p.closed)return;p.lastFrame=Date.now();video.requestVideoFrameCallback(frame);};
    video.requestVideoFrameCallback(frame);
  } else video.addEventListener('timeupdate',()=>{p.lastFrame=Date.now();});
  p.watchdog=setInterval(()=>{
    if(!p.closed&&!document.hidden&&Date.now()-p.lastFrame>18000)retryPlayer(id,p);
  },5000);
  if(!window.RTCPeerConnection){startHls(id,p);return;}
  startWebRtc(id,p);
}
function retryPlayer(id,p) {
  if(p.closed||p.retryTimer)return;
  const card=state.cards.get(id);if(card){card.placeholder.hidden=false;card.waiting.textContent='Reconnecting to body camera…';}
  p.retryTimer=setTimeout(()=>{stopPlayer(id);if(state.visible.has(id))ensurePlayer(id);},3500);
}
async function waitIce(pc) {
  if(pc.iceGatheringState==='complete')return;
  await new Promise(resolve=>{
    let timeout;
    function complete(){clearTimeout(timeout);pc.removeEventListener('icegatheringstatechange',change);resolve();}
    function change(){if(pc.iceGatheringState==='complete')complete();}
    pc.addEventListener('icegatheringstatechange',change);timeout=setTimeout(complete,1800);
  });
}
async function startWebRtc(id,p) {
  try {
    p.pc=new RTCPeerConnection({iceServers:[]});
    p.pc.addTransceiver('video',{direction:'recvonly'});p.pc.addTransceiver('audio',{direction:'recvonly'});
    p.pc.ontrack=e=>{if(!p.closed){p.video.srcObject=e.streams[0]||new MediaStream([e.track]);p.video.play().catch(()=>{});}};
    p.pc.onconnectionstatechange=()=>{if(p.pc.connectionState==='failed'&&!p.closed)startHls(id,p);};
    p.fallbackTimer=setTimeout(()=>{if(!p.closed&&p.video.readyState<2)startHls(id,p);},7000);
    await p.pc.setLocalDescription(await p.pc.createOffer());await waitIce(p.pc);if(p.closed||p.hlsMode)return;
    const endpoint=url('/api/streams/'+id+'/whep');
    const r=await api('/api/streams/'+id+'/whep',{method:'POST',headers:{'Content-Type':'application/sdp'},body:p.pc.localDescription.sdp,signal:p.controller.signal,raw:true});
    const location=r.headers.get('Location');if(location)p.session=new URL(location,endpoint).href;
    if(p.closed||p.hlsMode){if(p.session)fetch(p.session,{method:'DELETE',headers:{Authorization:'Bearer '+state.token}}).catch(()=>{});return;}
    await p.pc.setRemoteDescription({type:'answer',sdp:await r.text()});
  }catch(error){if(!p.closed&&error.name!=='AbortError')startHls(id,p);}
}
async function startHls(id,p) {
  if(p.closed||p.hlsMode)return;p.hlsMode=true;clearTimeout(p.fallbackTimer);
  if(p.pc)p.pc.close();
  if(p.session){fetch(p.session,{method:'DELETE',headers:{Authorization:'Bearer '+state.token}}).catch(()=>{});p.session=null;}
  p.video.srcObject=null;
  const card=state.cards.get(id);if(card){card.placeholder.hidden=false;card.waiting.textContent='Connecting through HLS…';}
  try {
    if(window.Hls&&Hls.isSupported()) {
      p.hls=new Hls({lowLatencyMode:true,liveSyncDurationCount:3,liveMaxLatencyDurationCount:7,
        backBufferLength:8,maxBufferLength:12,
        xhrSetup:xhr=>xhr.setRequestHeader('Authorization','Bearer '+state.token)});
      p.hls.loadSource(url('/api/streams/'+id+'/hls/index.m3u8'));p.hls.attachMedia(p.video);
      p.hls.on(Hls.Events.MANIFEST_PARSED,()=>p.video.play().catch(()=>{}));
      p.hls.on(Hls.Events.ERROR,(_,data)=>{if(!data.fatal||p.closed)return;
        if(data.type===Hls.ErrorTypes.MEDIA_ERROR&&!p.recovered){p.recovered=true;p.hls.recoverMediaError();}
        else retryPlayer(id,p);
      });
    } else if(p.video.canPlayType('application/vnd.apple.mpegurl')) {
      const data=await api('/api/streams/'+id+'/ticket',{method:'POST'});
      if(p.closed)return;p.video.src=url('/api/streams/'+id+'/hls/index.m3u8?ticket='+encodeURIComponent(data.ticket));
      p.video.addEventListener('error',()=>retryPlayer(id,p));p.video.play().catch(()=>{});
    } else {
      if(card){card.waiting.textContent='Browser cannot play this stream';card.detail.textContent='Use a browser with WebRTC or HLS support and configure H.264 on the camera.';}
    }
  }catch(error){if(!p.closed){if(card)card.detail.textContent=error.message;retryPlayer(id,p);}}
}
function resetSession() {
  for(const id of state.players.keys())stopPlayer(id);
  state.token='';state.role='';state.connected=false;state.mediaOnline=false;state.cameras=plannedCameras();
  sessionStorage.removeItem('bodycam_token');sessionStorage.removeItem('bodycam_role');
  $('auth-button').textContent='Connect server';$('server-status').textContent='Server not connected';
  $('server-dot').className='dot unknown';$('fleet-count').textContent='33 cameras';$('hours-label').textContent='';
  $('last-update').textContent='Connect the central server to receive live camera status.';renderGrid();
}
async function refresh() {
  if(!state.token||!state.base||state.refreshing)return;state.refreshing=true;
  try {
    const data=await api('/api/cameras');state.cameras=data.cameras;state.connected=true;state.mediaOnline=data.media_online;
    $('auth-button').textContent='Logout';$('server-status').textContent=data.media_online?'Central server connected':'Streaming server unavailable';
    $('server-dot').className='dot '+(data.media_online?'online':'unknown');
    $('fleet-count').textContent=data.summary.online+' / '+data.summary.registered+' live';
    $('hours-label').textContent='· '+data.summary.camera_hours_today.toFixed(1)+' camera-hours today';
    $('last-update').textContent='Updated '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+' IST · '+bytes(data.summary.storage_free_bytes)+' storage available';
    if(data.media_online)notice('');else notice('The backend is connected, but MediaMTX is unavailable. Check the streaming-server service.');
    renderGrid();
  }catch(error){
    if(error.status===401){resetSession();notice('Your session expired. Connect and sign in again.');}
    else {state.connected=false;state.cameras=state.cameras.map(c=>({...c,status:'unknown'}));$('server-status').textContent='Central server unreachable';$('server-dot').className='dot unknown';$('fleet-count').textContent='— / 33 live';notice('Cannot reach the central server. '+error.message);renderGrid();}
  }finally{state.refreshing=false;}
}
$('login-form').addEventListener('submit',async e=>{
  e.preventDefault();$('login-error').textContent='';$('login-submit').disabled=true;
  try {
    const u=new URL($('server-url').value);
    if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw new Error('Enter a server URL without credentials, query parameters or fragments.');
    if(location.protocol==='https:'&&u.protocol!=='https:')throw new Error('This hosted dashboard needs an HTTPS backend address. You can also open the server’s local dashboard directly.');
    const previous=state.base;state.base=u.href.replace(/\/+$/,'');
    let data;try{data=await api('/api/auth/login',{method:'POST',body:{username:$('username').value,password:$('password').value}});}catch(error){state.base=previous;throw error;}
    for(const id of state.players.keys())stopPlayer(id);
    state.token=data.token;state.role=data.role;localStorage.setItem('bodycam_api',state.base);sessionStorage.setItem('bodycam_token',state.token);sessionStorage.setItem('bodycam_role',state.role);
    $('password').value='';$('login-dialog').close();await refresh();
    const pending=state.pending;state.pending=null;if(pending)pending();
  }catch(error){$('login-error').textContent=error.message;}finally{$('login-submit').disabled=false;}
});
$('auth-button').addEventListener('click',async()=>{
  if(!state.token){openLogin();return;}
  try{await api('/api/auth/logout',{method:'POST'});}catch{}
  document.querySelectorAll('dialog[open]').forEach(d=>d.close());resetSession();notice('');
});
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{
  if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}
}));
$('filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(b){state.filter=b.dataset.filter;renderGrid();}});
$('search').addEventListener('input',e=>{state.query=e.target.value;renderGrid();});
$('server-button').addEventListener('click',openLogin);
$('manage-button').addEventListener('click',()=>openSetup(state.selected));
async function openSetup(id) {
  if(!requireLogin(()=>openSetup(id)))return;
  if(state.role!=='admin'){toast('Sign in as admin to manage cameras.');return;}
  state.selected=id;openDialog('setup-dialog');
  const list=$('camera-list');list.replaceChildren();
  state.cameras.forEach(c=>{const b=action(c.name,()=>openSetup(c.id),'camera-choice'+(c.id===id?' selected':''));list.append(b);});
  const cam=state.cameras.find(c=>c.id===id);if(!cam)return;
  $('setup-title').textContent=cam.name;$('cam-name').value=cam.name;$('cam-group').value=cam.group;
  $('cam-officer').value=cam.officer;$('cam-model').value=cam.model;$('cam-mode').value=cam.mode;
  $('cam-enabled').checked=cam.enabled;$('cam-record').checked=cam.recording_enabled;
  $('cam-rtsp').value='';$('cam-rtsp').placeholder=cam.source_configured?'Source already saved. Leave blank to keep it.':'rtsp://user:password@vpn-camera-ip:554/path';
  $('save-result').textContent='';$('probe-result').textContent='';modeFields();
  ['rtmp-url','rtmp-server','rtmp-key','srt-url','rtsp-publish-url','publish-user','publish-password'].forEach(x=>$(x).value='');
  try{const data=await api('/api/cameras/'+id+'/connection');if(state.selected!==id)return;
    $('rtmp-url').value=data.rtmp_url;$('rtmp-server').value=data.rtmp_server;$('rtmp-key').value=data.rtmp_stream_key;
    $('srt-url').value=data.srt_url;$('rtsp-publish-url').value=data.rtsp_publish_url;$('publish-user').value=data.username;$('publish-password').value=data.password;
  }catch(error){toast(error.message);}
}
function modeFields(){$('rtsp-fields').hidden=$('cam-mode').value!=='rtsp';$('connection-panel').hidden=$('cam-mode').value!=='publish';}
$('cam-mode').addEventListener('change',modeFields);
$('camera-form').addEventListener('submit',async e=>{
  e.preventDefault();$('save-camera').disabled=true;$('save-result').textContent='';
  try {
    const data={name:$('cam-name').value.trim(),group:$('cam-group').value.trim(),officer:$('cam-officer').value.trim(),model:$('cam-model').value.trim(),
      mode:$('cam-mode').value,enabled:$('cam-enabled').checked,recording_enabled:$('cam-record').checked};
    if($('cam-rtsp').value)data.rtsp_url=$('cam-rtsp').value;
    const result=await api('/api/cameras/'+state.selected,{method:'PATCH',body:data});$('cam-rtsp').value='';
    await refresh();$('save-result').textContent=result.message;toast(result.message);
  }catch(error){$('save-result').textContent=error.message;}finally{$('save-camera').disabled=false;}
});
$('copy-url').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('rtmp-url').value);toast('Camera publishing URL copied.');}catch{const box=$('rtmp-url');box.focus();box.select();toast('Select and copy the camera URL.');}});
$('rotate-key').addEventListener('click',async()=>{try{await api('/api/cameras/'+state.selected+'/rotate-key',{method:'POST'});await openSetup(state.selected);toast('New key generated. Update this camera’s publishing settings.');}catch(error){toast(error.message);}});
$('probe-button').addEventListener('click',async()=>{const b=$('probe-button');b.disabled=true;$('probe-result').textContent='Testing connection…';try{const data=await api('/api/cameras/'+state.selected+'/probe',{method:'POST'});$('probe-result').textContent=data.reachable?'Video received: '+data.video.codec_name+' · '+data.video.width+'×'+data.video.height:data.message;}catch(error){$('probe-result').textContent=error.message;}finally{b.disabled=false;}});
$('setup-dialog').addEventListener('close',()=>{['rtmp-url','rtmp-server','rtmp-key','srt-url','rtsp-publish-url','publish-password','cam-rtsp'].forEach(id=>$(id).value='');});
$('recordings-button').addEventListener('click',()=>{
  if(!requireLogin(()=>$('recordings-button').click()))return;
  const selector=$('recording-camera');selector.replaceChildren(element('option','','All cameras'));selector.firstChild.value='';
  state.cameras.forEach(cam=>{const o=element('option','',cam.name);o.value=cam.id;selector.append(o);});
  state.recordingOffset=0;openDialog('recordings-dialog');loadRecordings();
});
async function loadRecordings() {
  const list=$('recording-list');list.replaceChildren(element('div','empty-state','Loading recordings…'));
  try{
    const params=new URLSearchParams({camera:$('recording-camera').value,q:$('recording-search').value,offset:state.recordingOffset,limit:50});
    const data=await api('/api/recordings?'+params);state.recordingTotal=data.total;list.replaceChildren();
    $('recording-summary').textContent=data.total+' matching recordings · '+bytes(data.total_size_bytes)+' total archive storage';
    if(!data.items.length)list.append(element('div','empty-state','No recorded video files found. Recording begins when a camera’s live stream reaches the server.'));
    for(const item of data.items){
      const row=element('div','recording-row'),description=element('div');
      description.append(element('h3','',item.camera_name),element('p','',timestamp(item.started_at)+' · '+bytes(item.size_bytes)+(item.in_progress?' · Recording / completion pending':'')),element('p','',item.filename));
      const buttons=element('div','recording-actions');buttons.append(action('View',()=>playRecording(item)),action('Download',()=>downloadRecording(item)));
      buttons.lastChild.disabled=item.in_progress;row.append(description,buttons);list.append(row);
    }
    $('recordings-prev').disabled=state.recordingOffset===0;$('recordings-next').disabled=state.recordingOffset+50>=data.total;
    $('recording-page').textContent=data.total?String(state.recordingOffset+1)+'–'+Math.min(state.recordingOffset+50,data.total)+' of '+data.total:'No recordings';
  }catch(error){list.replaceChildren(element('div','empty-state',error.message));}
}
$('recording-camera').addEventListener('change',()=>{state.recordingOffset=0;loadRecordings();});
let recordingSearchTimer;$('recording-search').addEventListener('input',()=>{clearTimeout(recordingSearchTimer);recordingSearchTimer=setTimeout(()=>{state.recordingOffset=0;loadRecordings();},300);});
$('refresh-recordings').addEventListener('click',loadRecordings);
$('recordings-prev').addEventListener('click',()=>{state.recordingOffset=Math.max(0,state.recordingOffset-50);loadRecordings();});
$('recordings-next').addEventListener('click',()=>{state.recordingOffset+=50;loadRecordings();});
async function playRecording(item) {
  try{
    const data=await api('/api/recordings/'+item.id+'/ticket',{method:'POST'});
    $('playback-title').textContent=item.camera_name+' · '+timestamp(item.started_at);
    $('playback-help').textContent=item.in_progress?'Recording completion is pending. If playback fails, view a completed segment.':'Original camera video. H.265 recordings may need a compatible player or a downloaded copy.';
    $('recording-video').src=url(data.url);openDialog('playback-dialog');$('recording-video').play().catch(()=>{});
  }catch(error){toast(error.message);}
}
async function downloadRecording(item) {try{const data=await api('/api/recordings/'+item.id+'/ticket',{method:'POST'});const a=element('a');a.href=url(data.url+'&download=true');a.rel='noreferrer';a.target='_blank';a.click();}catch(error){toast(error.message);}}
$('playback-dialog').addEventListener('close',()=>{const v=$('recording-video');v.pause();v.removeAttribute('src');v.load();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refresh();for(const id of state.visible)ensurePlayer(id);}});
document.addEventListener('fullscreenchange',()=>{for(const [id,p] of state.players)p.video.controls=document.fullscreenElement===state.cards.get(id)?.card;});
window.addEventListener('beforeunload',()=>{for(const id of state.players.keys())stopPlayer(id);});
renderGrid();refresh();setInterval(refresh,10000);
