"use strict";
const config = JSON.parse(document.getElementById('playbackConfig').textContent);
const el = id => document.getElementById(id);
const playerShell=el('playerShell'), video=el('video'), message=el('playerMessage'),
 playerControls=el('playerControls'), togglePlay=el('togglePlay'), seek=el('seek'),
 currentLabel=el('currentLabel'), audioSelect=el('audioSelect'), subtitleSelect=el('subtitleSelect'),
 qualitySelect=el('qualitySelect'), customQuality=el('customQuality'),
 options=el('playbackOptions'), stats=el('playbackStats');
const totalDuration=config.duration;
const videoCodecSelect=el('videoCodecSelect'), audioCodecSelect=el('audioCodecSelect');
let videoCodec='auto', audioCodec='auto', codecCapabilities=null, codecLoading=false, lastTechnical=null;
const videoCodecNames={h264:'H.264 / AVC',hevc:'H.265 / HEVC',av1:'AV1'};
const audioCodecNames={aac:'AAC',opus:'Opus',mp3:'MP3'};
let quality=qualitySelect.value, custom={height:720,bitrate:4000,audio:128};
let streamIsDirect=false, logicalOffset=0, started=false, dragging=false, forceTranscode=false;
let controlsTimer=null, controlsHovered=false, playbackId='', lastFrameSample=null;
let statsTimer=null, statsBusy=false, statsRequest=null;

function fmt(sec){sec=Math.max(0,Math.floor(sec||0));const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`;}
function usingDirect(){return config.native&&(!config.nativeMime||video.canPlayType(config.nativeMime)!=='')&&quality==='auto'&&videoCodec==='auto'&&audioCodec==='auto'&&!forceTranscode&&(!audioSelect||audioSelect.selectedIndex===0);}
function logicalTime(){return streamIsDirect?(video.currentTime||0):logicalOffset+(video.currentTime||0);}
function audio(){return audioSelect?audioSelect.value:'';}
function msg(text){message.textContent=text;message.classList.toggle('hidden',!text);}
function newRequest(){
 playbackId=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
 lastFrameSample=null;lastTechnical=null;
 for(const id of ['statEncoder','statBandwidth','statTransferred'])el(id).textContent='—';
}
function updateQualityInfo(){
 const direct=usingDirect(), profile=config.profiles[quality];
 el('playbackMode').textContent=direct?'Direct play':quality==='custom'?`Custom · ${custom.height}p`:`Transcode · ${profile.height?profile.height+'p':'original'}`;
 el('qualityDescription').textContent=direct?'Original file · no conversion':quality==='custom'?`Up to ${custom.height}p · video ${custom.bitrate/1000} Mbps + audio ${custom.audio} Kbps`:profile.video_kbps?`Up to ${profile.height}p · video ${profile.video_kbps/1000} Mbps + audio ${profile.audio_kbps} Kbps`:'Source resolution · variable bitrate';
}
function subtitles(start){
 video.querySelectorAll('track').forEach(track=>track.remove());
 if(!subtitleSelect||!subtitleSelect.value)return;
 const track=document.createElement('track');track.kind='subtitles';track.label=subtitleSelect.selectedOptions[0].textContent;track.srclang='und';track.default=true;
 track.src=config.subtitleUrl+'?'+new URLSearchParams({stream:subtitleSelect.value,start});video.appendChild(track);
}
function transcode(at,autoplay=true){
 logicalOffset=Math.max(0,totalDuration>0?Math.min(totalDuration,at):at);newRequest();subtitles(logicalOffset);msg('Starting stream…');
 const params=new URLSearchParams({start:logicalOffset,audio:audio(),quality,video_codec:videoCodec,audio_codec:audioCodec,playback_id:playbackId});
 if(quality==='custom'){params.set('height',custom.height);params.set('bitrate',custom.bitrate);params.set('audio_bitrate',custom.audio);}
 video.src=config.transcodeUrl+'?'+params;video.load();if(autoplay)video.play().catch(()=>{});
}
let metadataSeek=null;
function start(at=0,autoplay=true){
 if(metadataSeek){video.removeEventListener('loadedmetadata',metadataSeek);metadataSeek=null;}
 streamIsDirect=usingDirect();updateQualityInfo();
 if(streamIsDirect){
  newRequest();subtitles(0);video.src=config.nativeUrl+'?'+new URLSearchParams({playback_id:playbackId});video.load();
  metadataSeek=()=>{metadataSeek=null;try{video.currentTime=at;}catch(_){}if(autoplay)video.play().catch(()=>{});};
  if(video.readyState>=1)metadataSeek();else video.addEventListener('loadedmetadata',metadataSeek,{once:true});
 }else transcode(at,autoplay);
 started=true;
}
function go(at){if(totalDuration<=0)return;at=Math.max(0,Math.min(totalDuration,at));if(!started)return start(at);streamIsDirect?video.currentTime=at:transcode(at,!video.paused);}
function switchStream(change){const at=logicalTime(),autoplay=!video.paused;change();started=false;logicalOffset=0;start(at,autoplay);showControls();}
function fullscreenElement(){return document.fullscreenElement||document.webkitFullscreenElement||null;}
function isFullscreen(){return fullscreenElement()===playerShell;}
async function toggleFullscreen(){try{if(isFullscreen()){if(document.exitFullscreen)await document.exitFullscreen();else if(document.webkitExitFullscreen)document.webkitExitFullscreen();}else{if(playerShell.requestFullscreen)await playerShell.requestFullscreen();else if(playerShell.webkitRequestFullscreen)playerShell.webkitRequestFullscreen();}}catch(_){}}
function clearControlsTimer(){if(controlsTimer!==null){clearTimeout(controlsTimer);controlsTimer=null;}}
function scheduleControlsHide(){
 clearControlsTimer();
 if(video.paused||dragging||controlsHovered||!options.hidden||!stats.hidden||playerShell.querySelector(':focus-visible'))return;
 controlsTimer=setTimeout(()=>{if(!video.paused&&!dragging&&options.hidden&&stats.hidden)playerShell.classList.add('controls-hidden');},2500);
}
function showControls(){playerShell.classList.remove('controls-hidden');scheduleControlsHide();}
function updateFullscreenState(){const active=isFullscreen();playerShell.classList.toggle('is-fullscreen',active);el('fullscreenTop').setAttribute('aria-label',active?'Exit fullscreen':'Enter fullscreen');showControls();}
function closeOptions(restoreFocus=false){options.hidden=true;el('optionsToggle').setAttribute('aria-expanded','false');if(restoreFocus)el('optionsToggle').focus();showControls();}
function closeStats(restoreFocus=false){
 stats.hidden=true;el('infoToggle').setAttribute('aria-expanded','false');
 clearInterval(statsTimer);statsTimer=null;statsRequest?.abort();lastFrameSample=null;
 if(restoreFocus)el('infoToggle').focus();showControls();
}
el('optionsToggle').onclick=()=>{if(!options.hidden)return closeOptions();closeStats();options.hidden=false;el('optionsToggle').setAttribute('aria-expanded','true');showControls();qualitySelect.focus();loadCodecOptions();};
el('optionsClose').onclick=()=>closeOptions(true);
el('infoToggle').onclick=()=>{if(!stats.hidden)return closeStats();closeOptions();stats.hidden=false;el('infoToggle').setAttribute('aria-expanded','true');showControls();el('infoClose').focus();refreshStats();statsTimer=setInterval(refreshStats,1000);};
el('infoClose').onclick=()=>closeStats(true);

togglePlay.onclick=()=>{showControls();if(!started||video.ended)return start(0);video.paused?video.play().catch(()=>{}):video.pause();};
el('back30').onclick=()=>{showControls();go(logicalTime()-30);};
el('forward30').onclick=()=>{showControls();go(logicalTime()+30);};
video.addEventListener('playing',()=>{msg('');togglePlay.textContent='Ⅱ';togglePlay.setAttribute('aria-label','Pause');scheduleControlsHide();});
video.addEventListener('loadeddata',()=>{if(video.paused)msg('');});
video.addEventListener('pause',()=>{togglePlay.textContent='▶';togglePlay.setAttribute('aria-label','Play');showControls();clearControlsTimer();});
video.addEventListener('waiting',()=>{if(started)msg('Buffering…');});
video.addEventListener('error',()=>{if(streamIsDirect&&!forceTranscode){switchStream(()=>{forceTranscode=true;});return;}msg('Playback failed. Check the Activity page or try another quality.');});
video.addEventListener('ended',()=>{showControls();msg('Playback finished');});
seek.oninput=()=>{dragging=true;showControls();currentLabel.textContent=fmt(Number(seek.value));};
seek.onchange=()=>{dragging=false;go(Number(seek.value));scheduleControlsHide();};
video.addEventListener('timeupdate',()=>{if(dragging)return;const at=totalDuration>0?Math.min(totalDuration,logicalTime()):logicalTime();seek.value=at;currentLabel.textContent=fmt(at);});
if(totalDuration<=0){seek.disabled=true;el('back30').disabled=true;el('forward30').disabled=true;document.querySelector('.duration-label').textContent='Live / unknown';}
el('volume').oninput=()=>{video.volume=Number(el('volume').value);showControls();};
el('fullscreenTop').onclick=toggleFullscreen;
qualitySelect.onchange=()=>{customQuality.hidden=qualitySelect.value!=='custom';if(qualitySelect.value!=='custom')switchStream(()=>{quality=qualitySelect.value;forceTranscode=false;});};
el('applyQuality').onclick=()=>{if(!el('customBitrate').reportValidity())return;switchStream(()=>{quality='custom';custom={height:Number(el('customHeight').value),bitrate:Number(el('customBitrate').value),audio:Number(el('customAudio').value)};forceTranscode=false;});};
if(audioSelect)audioSelect.onchange=()=>switchStream(()=>{});
if(subtitleSelect)subtitleSelect.onchange=()=>{showControls();if(started)streamIsDirect?subtitles(0):go(logicalTime());};

function supportsCodec(codec){return video.canPlayType(`video/mp4; codecs="${codec}"`)!=='';}
function codecHint(){
 const selected=codecCapabilities?.video.find(item=>item.id===videoCodec);
 el('codecStatus').textContent='Auto preserves direct play when possible. Choosing a codec forces transcoding.'+(selected?.backend==='software'&&selected.id!=='h264'?' This video codec uses software encoding.':'');
}
async function loadCodecOptions(){
 if(codecCapabilities||codecLoading)return;
 codecLoading=true;el('codecStatus').textContent='Checking server encoders and browser support…';el('retryCodecs').hidden=true;
 try{
  const response=await fetch(config.codecsUrl,{cache:'no-store'});
  if(!response.ok)throw new Error('Codec detection failed');
  const capabilities=await response.json();
  for(const [select,items] of [[videoCodecSelect,capabilities.video],[audioCodecSelect,capabilities.audio]]){
   if(!select)continue;
   for(const item of items){
    const option=document.createElement('option');option.value=item.id;
    const browserOK=supportsCodec(item.browser_codec);
    option.disabled=!item.available||!browserOK;
    option.textContent=item.label+(!item.available?' · unavailable on server':!browserOK?' · unsupported by browser':item.backend==='software'?' · software':'');
    select.appendChild(option);
   }
  }
  codecCapabilities=capabilities;codecHint();
 }catch(_){el('codecStatus').textContent='Codec detection unavailable. Auto playback is still available.';el('retryCodecs').hidden=false;}
 finally{codecLoading=false;}
}
function changeCodecs(){
 const wantVideo=videoCodecSelect.value,wantAudio=audioCodecSelect?.value||'auto';
 const v=codecCapabilities?.video.find(item=>item.id===(wantVideo==='auto'?'h264':wantVideo));
 const a=codecCapabilities?.audio.find(item=>item.id===(wantAudio==='auto'?'aac':wantAudio));
 const codecString=[v?.browser_codec,...(audioSelect?[a?.browser_codec]:[])].filter(Boolean).join(', ');
 if(codecString&&!supportsCodec(codecString)){
  videoCodecSelect.value=videoCodec;if(audioCodecSelect)audioCodecSelect.value=audioCodec;
  el('codecStatus').textContent='This video/audio combination is unsupported by your browser. Choose another codec.';return;
 }
 switchStream(()=>{videoCodec=wantVideo;audioCodec=wantAudio;forceTranscode=false;});codecHint();
}
videoCodecSelect.onchange=changeCodecs;
if(audioCodecSelect)audioCodecSelect.onchange=changeCodecs;
el('retryCodecs').onclick=loadCodecOptions;

function put(id,value){el(id).textContent=value===null||value===undefined||value===''?'—':String(value);}
function codec(stream){return stream?.codec_name?.toUpperCase()||'—';}
function bitrate(value){return Number(value)>0?`${(Number(value)/1000000).toFixed(2)} Mbps`:'—';}
function sourceFps(value){if(!value)return '—';const [a,b=1]=String(value).split('/').map(Number);return b&&Number.isFinite(a/b)?`${(a/b).toFixed(3).replace(/\.?0+$/,'')} fps`:'—';}
function updateLocalStats(){
 const source=config.media, originalVideo=source.video, selectedAudio=source.audios.find(track=>String(track.index)===audio()), profile=config.profiles[quality];
 put('statMethod',streamIsDirect?'Direct play':'Transcoding');
 put('statReason',streamIsDirect?'Browser supports the original file':forceTranscode?'Direct playback failed':videoCodec!=='auto'||audioCodec!=='auto'?'Selected video / audio codec':quality!=='auto'?'Selected quality profile':audioSelect&&audioSelect.selectedIndex>0?'Selected audio track':'Source requires conversion');
 put('statVideoCodec',streamIsDirect?codec(originalVideo):videoCodecNames[lastTechnical?.video_codec||(videoCodec==='auto'?'h264':videoCodec)]);
 put('statAudioCodec',!selectedAudio?'No audio':streamIsDirect?`${codec(selectedAudio)} · ${selectedAudio.channels||'?'} ${selectedAudio.channels===1?'channel':'channels'}`:`${audioCodecNames[lastTechnical?.audio_codec||(audioCodec==='auto'?'aac':audioCodec)]} · stereo · 48 kHz`);
 put('statResolution',video.videoWidth&&video.videoHeight?`${video.videoWidth} × ${video.videoHeight}`:'Waiting for metadata');
 const videoKbps=quality==='custom'?custom.bitrate:profile?.video_kbps;
 put('statVideoTarget',streamIsDirect?'Original (no conversion)':videoKbps?`${videoKbps/1000} Mbps`:'Variable bitrate (no cap)');
 put('statAudioTarget',!selectedAudio?'No audio':streamIsDirect?'Original':`${quality==='custom'?custom.audio:profile.audio_kbps} Kbps`);
 if(streamIsDirect)put('statEncoder','None · direct delivery');
 put('statSubtitle',subtitleSelect?.value?subtitleSelect.selectedOptions[0].textContent:'Off');
 put('statState',video.error?'Playback error':video.ended?'Ended':video.paused?'Paused':video.readyState<3?'Buffering':'Playing');
 let ahead=0;for(let i=0;i<video.buffered.length;i++){if(video.currentTime>=video.buffered.start(i)&&video.currentTime<=video.buffered.end(i)){ahead=video.buffered.end(i)-video.currentTime;break;}}
 put('statBuffer',`${ahead.toFixed(1)} s`);
 const frames=video.getVideoPlaybackQuality?.(), now=performance.now();
 if(frames){
  const displayed=frames.totalVideoFrames-frames.droppedVideoFrames;
  const fps=lastFrameSample&&displayed>=lastFrameSample.frames&&!video.paused?(displayed-lastFrameSample.frames)*1000/(now-lastFrameSample.time):null;
  put('statFps',fps!==null&&Number.isFinite(fps)?`${fps.toFixed(1)} fps`:'—');
  put('statDropped',`${frames.droppedVideoFrames} / ${frames.totalVideoFrames}`);lastFrameSample={frames:displayed,time:now};
 }else{put('statFps',null);put('statDropped',null);}
 put('statContainer',source.container);put('statSourceVideo',`${codec(originalVideo)}${originalVideo.profile?' · '+originalVideo.profile:''}`);
 put('statSourceResolution',originalVideo.width&&originalVideo.height?`${originalVideo.width} × ${originalVideo.height}`:null);
 put('statSourceFps',sourceFps(originalVideo.avg_frame_rate));put('statPixelFormat',originalVideo.pix_fmt);put('statColor',originalVideo.color_transfer);
 put('statSourceAudio',selectedAudio?`${codec(selectedAudio)} · ${selectedAudio.channels||'?'} ch${selectedAudio.sample_rate?' · '+Number(selectedAudio.sample_rate)/1000+' kHz':''}`:'No audio');
 put('statSourceBitrate',bitrate(source.bit_rate));
}
async function refreshStats(){
 if(stats.hidden||document.hidden)return;
 updateLocalStats();if(statsBusy||!playbackId)return;
 statsBusy=true;const token=playbackId;statsRequest=new AbortController();
 try{
  const response=await fetch(config.statsUrl+'?'+new URLSearchParams({playback_id:token}),{cache:'no-store',signal:statsRequest.signal});
  if(!response.ok)throw new Error('Stats unavailable');
  const data=await response.json();if(token!==playbackId||stats.hidden)return;
  const stream=data.stream;
  if(stream){
   lastTechnical=stream.technical||null;
   if(!streamIsDirect){put('statEncoder',lastTechnical?.encoder_label||stream.detail.split(' · ')[0]);if(lastTechnical?.fallback)put('statReason',lastTechnical.fallback);}
   put('statBandwidth',`${stream.bandwidth_mbps.toFixed(2)} Mbps${stream.active?'':' · request completed'}`);
   put('statTransferred',`${(stream.bytes_sent/1048576).toFixed(2)} MiB`);
  }else{if(!streamIsDirect)put('statEncoder','Waiting for encoder');put('statBandwidth','—');put('statTransferred','—');}
 }catch(error){if(error.name!=='AbortError'&&!stats.hidden&&token===playbackId){if(!streamIsDirect)put('statEncoder','Server metrics unavailable');put('statBandwidth','—');put('statTransferred','—');}}
 finally{statsBusy=false;statsRequest=null;}
}
['pointermove','pointerdown','touchstart'].forEach(type=>playerShell.addEventListener(type,showControls,{passive:true}));
playerControls.addEventListener('pointerenter',()=>{controlsHovered=true;clearControlsTimer();playerShell.classList.remove('controls-hidden');});
playerControls.addEventListener('pointerleave',()=>{controlsHovered=false;scheduleControlsHide();});
playerShell.addEventListener('focusin',()=>{clearControlsTimer();playerShell.classList.remove('controls-hidden');});
playerShell.addEventListener('focusout',scheduleControlsHide);
playerShell.addEventListener('click',event=>{if(!options.hidden&&!options.contains(event.target)&&!el('optionsToggle').contains(event.target))closeOptions();});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'){if(!options.hidden){event.preventDefault();closeOptions(true);}if(!stats.hidden){event.preventDefault();closeStats(true);}return;}
 if(['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName))return;
 if(event.key.toLowerCase()==='f'){event.preventDefault();toggleFullscreen();}
 else if(event.key.toLowerCase()==='i'){event.preventDefault();el('infoToggle').click();}
 else if(event.key===' '&&event.target.tagName!=='BUTTON'){event.preventDefault();togglePlay.click();}
 showControls();
});
document.addEventListener('fullscreenchange',updateFullscreenState);
document.addEventListener('webkitfullscreenchange',updateFullscreenState);
document.addEventListener('visibilitychange',()=>{lastFrameSample=null;if(!document.hidden)refreshStats();});
window.addEventListener('pagehide',()=>{clearInterval(statsTimer);clearControlsTimer();statsRequest?.abort();});
start(0);updateFullscreenState();
