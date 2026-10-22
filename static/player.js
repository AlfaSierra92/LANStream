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
let seekTimer=null, pendingSeek=null;
let playbackBlocked=false;
const subtitleWindows=new WindowedSubtitles(video,config.subtitleUrl,el('subtitleStatus'),totalDuration);

function fmt(sec){sec=Math.max(0,Math.floor(sec||0));const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`;}
function usingDirect(){return quality==='source'||(config.native&&(!config.nativeMime||video.canPlayType(config.nativeMime)!=='')&&quality==='auto'&&videoCodec==='auto'&&audioCodec==='auto'&&!forceTranscode&&(!audioSelect||audioSelect.selectedIndex===0));}
function logicalTime(){return pendingSeek!==null?pendingSeek:streamIsDirect?(video.currentTime||0):logicalOffset+(video.currentTime||0);}
function audio(){return audioSelect?audioSelect.value:'';}
function nativeHls(){return video.canPlayType('application/vnd.apple.mpegurl')!=='';}
function useHls(){return nativeHls()&&totalDuration>0&&quality!=='audio_only'&&['auto','h264'].includes(videoCodec)&&['auto','aac'].includes(audioCodec);}
function msg(text){message.textContent=text;message.classList.toggle('hidden',!text);}
function requestPlay(){
 const token=playbackId;
 playbackBlocked=false;
 const result=video.play();
 if(result)result.catch(error=>{
  if(token!==playbackId||error.name==='AbortError')return;
  if(error.name==='NotAllowedError'){
   playbackBlocked=true;msg('Tap ▶ to start playback.');showControls();
  }else if(!video.error){msg('Unable to start playback. Tap ▶ to retry.');showControls();}
 });
}
function newRequest(){
 playbackBlocked=false;
 playbackId=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
 lastFrameSample=null;lastTechnical=null;
 for(const id of ['statEncoder','statBandwidth','statTransferred'])el(id).textContent='—';
}
function updateQualityInfo(){
 const direct=usingDirect(), profile=config.profiles[quality];
 for(const select of [videoCodecSelect,audioCodecSelect,audioSelect])if(select)select.disabled=quality==='source';
 videoCodecSelect.disabled=quality==='source'||quality==='audio_only';
 videoCodecSelect.value=quality==='audio_only'?'copy':videoCodec;
 codecHint();
 el('playbackMode').textContent=direct?'Direct play':quality==='audio_only'?'Original video · audio conversion':quality==='custom'?`Custom · ${custom.height}p`:`Transcode · ${profile.height?profile.height+'p':'original'}`;
 el('qualityDescription').textContent=direct?'Original file · no conversion':quality==='audio_only'?'Video unchanged · audio conversion only · browser must support original video and HDR':quality==='custom'?`Up to ${custom.height}p · video ${custom.bitrate/1000} Mbps + audio ${custom.audio} Kbps`:profile.video_kbps?`Up to ${profile.height}p · video ${profile.video_kbps/1000} Mbps + audio ${profile.audio_kbps} Kbps`:'Source resolution · variable bitrate';
}
function subtitles(start,at=start){
 subtitleWindows.select(subtitleSelect?.value||'',start,at);
}
function transcode(at,autoplay=true){
 cancelPendingSeek();
 if(quality==='source'){msg('Transcoding is disabled. Choose Auto to allow conversion.');return;}
 logicalOffset=Math.max(0,totalDuration>0?Math.min(totalDuration,at):at);newRequest();subtitles(logicalOffset);msg('Starting stream…');
 const params=new URLSearchParams({start:logicalOffset,audio:audio(),quality,video_codec:quality==='audio_only'?'auto':videoCodec,audio_codec:audioCodec,playback_id:playbackId});
 if(useHls())params.set('format','hls');
 if(quality==='custom'){params.set('height',custom.height);params.set('bitrate',custom.bitrate);params.set('audio_bitrate',custom.audio);}
 video.src=config.transcodeUrl+'?'+params;video.load();if(autoplay)requestPlay();
}
let metadataSeek=null;
function start(at=0,autoplay=true){
 cancelPendingSeek();
 if(metadataSeek){video.removeEventListener('loadedmetadata',metadataSeek);metadataSeek=null;}
 if(quality==='source'){videoCodec='auto';audioCodec='auto';videoCodecSelect.value='auto';if(audioCodecSelect)audioCodecSelect.value='auto';if(audioSelect)audioSelect.selectedIndex=0;forceTranscode=false;}
 streamIsDirect=usingDirect();updateQualityInfo();
 if(streamIsDirect){
  newRequest();subtitles(0,at);video.src=config.nativeUrl+'?'+new URLSearchParams({playback_id:playbackId});video.load();
  metadataSeek=()=>{metadataSeek=null;if(at>0)try{video.currentTime=at;}catch(_){}};
  if(video.readyState>=1)metadataSeek();else video.addEventListener('loadedmetadata',metadataSeek,{once:true});
  // Keep play() in the click handler's call stack: iOS requires a user gesture.
  if(autoplay)requestPlay();
 }else transcode(at,autoplay);
 started=true;
}
function cancelPendingSeek(){if(seekTimer!==null)clearTimeout(seekTimer);seekTimer=null;pendingSeek=null;}
function go(at){
 if(totalDuration<=0)return;
 at=Math.max(0,Math.min(totalDuration,at));cancelPendingSeek();
 if(!started)return start(at);
 if(streamIsDirect){video.currentTime=at;return;}
 const local=at-logicalOffset;
 // Reuse the current encoder when the browser already has this part. Leave a
 // small margin at range ends, where a seek could immediately stall again.
 for(let i=0;i<video.buffered.length;i++){
  if(local>=video.buffered.start(i)&&local+0.1<video.buffered.end(i)){
   try{video.currentTime=local;return;}catch(_){}
  }
 }
 // Collapse repeated skip clicks/seek changes before reopening a slow mount.
 pendingSeek=at;seek.value=at;currentLabel.textContent=fmt(at);
 seekTimer=setTimeout(()=>{const target=pendingSeek;cancelPendingSeek();transcode(target,!video.paused);},200);
}
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

togglePlay.onclick=()=>{showControls();if(!started||video.ended)return start(0);video.paused?requestPlay():video.pause();};
el('back30').onclick=()=>{showControls();go(logicalTime()-30);};
el('forward30').onclick=()=>{showControls();go(logicalTime()+30);};
video.addEventListener('playing',()=>{msg('');togglePlay.textContent='Ⅱ';togglePlay.setAttribute('aria-label','Pause');scheduleControlsHide();});
video.addEventListener('loadeddata',()=>{if(video.paused&&!playbackBlocked)msg('');});
video.addEventListener('pause',()=>{togglePlay.textContent='▶';togglePlay.setAttribute('aria-label','Play');showControls();clearControlsTimer();});
video.addEventListener('waiting',()=>{if(started&&!playbackBlocked&&!video.paused)msg('Buffering…');});
async function reportPlaybackFailure(){
 const token=playbackId;
 msg('Playback failed. Reading the server error…');showControls();
 for(let attempt=0;attempt<3;attempt++){
  if(attempt)await new Promise(resolve=>setTimeout(resolve,attempt*250));
  if(token!==playbackId||streamIsDirect||!video.error)return;
  try{
   const response=await fetch(config.statsUrl+'?'+new URLSearchParams({playback_id:token}),{cache:'no-store'});
   if(!response.ok)break;
   const data=await response.json();
   if(token!==playbackId||!video.error)return;
   const detail=data.stream?.technical?.error;
   if(detail){msg('Playback failed: '+detail);put('statReason',detail);return;}
  }catch(_){break;}
 }
 if(token===playbackId&&video.error)msg(quality==='audio_only'?'Playback failed. The browser must support the original video codec and HDR format. Video conversion is disabled in this mode.':'Playback failed. No encoder error was reported; check browser codec support and the server log.');
}
video.addEventListener('error',()=>{if(quality==='source'){msg('Original playback failed. Transcoding is disabled; choose Auto to allow conversion.');return;}if(streamIsDirect&&!forceTranscode){switchStream(()=>{forceTranscode=true;});return;}reportPlaybackFailure();});
video.addEventListener('ended',()=>{showControls();msg('Playback finished');});
seek.oninput=()=>{dragging=true;showControls();currentLabel.textContent=fmt(Number(seek.value));};
seek.onchange=()=>{dragging=false;go(Number(seek.value));scheduleControlsHide();};
video.addEventListener('timeupdate',()=>{if(dragging)return;const at=totalDuration>0?Math.min(totalDuration,logicalTime()):logicalTime();seek.value=at;currentLabel.textContent=fmt(at);});
if(totalDuration<=0){seek.disabled=true;el('back30').disabled=true;el('forward30').disabled=true;document.querySelector('.duration-label').textContent='Live / unknown';}
el('volume').oninput=()=>{video.volume=Number(el('volume').value);showControls();};
el('fullscreenTop').onclick=toggleFullscreen;
qualitySelect.onchange=()=>{customQuality.hidden=qualitySelect.value!=='custom';if(qualitySelect.value!=='custom'){switchStream(()=>{quality=qualitySelect.value;forceTranscode=false;});loadCodecOptions();}};
el('applyQuality').onclick=()=>{if(!el('customBitrate').reportValidity())return;switchStream(()=>{quality='custom';custom={height:Number(el('customHeight').value),bitrate:Number(el('customBitrate').value),audio:Number(el('customAudio').value)};forceTranscode=false;});};
if(audioSelect)audioSelect.onchange=()=>switchStream(()=>{});
if(subtitleSelect)subtitleSelect.onchange=()=>{showControls();if(started)subtitles(streamIsDirect?0:logicalOffset,logicalTime());};

function supportsCodec(codec){return video.canPlayType(`video/mp4; codecs="${codec}"`)!=='';}
function codecHint(){
 if(quality==='audio_only'){el('codecStatus').textContent='Video passthrough · no video encoding or tone mapping. Audio is converted; the browser must support the original video codec and HDR format.';return;}
 if(quality==='source'){el('codecStatus').textContent='Original media · transcoding disabled. Choose Auto to enable conversion controls.';return;}
 const selected=codecCapabilities?.video.find(item=>item.id===videoCodec);
 el('codecStatus').textContent='Auto preserves direct play when possible. Choosing a codec forces transcoding.'+(selected?.backend==='software'&&selected.id!=='h264'?' This video codec uses software encoding.':'');
}
async function loadCodecOptions(){
 if(quality==='source'){codecHint();return;}
 if((codecCapabilities&&(quality==='audio_only'||codecCapabilities.video.length))||codecLoading)return;
 const audioOnly=quality==='audio_only';
 codecLoading=true;el('codecStatus').textContent='Checking server encoders and browser support…';el('retryCodecs').hidden=true;
 try{
  const response=await fetch(config.codecsUrl+(audioOnly?'?audio_only=1':''),{cache:'no-store'});
  if(!response.ok)throw new Error('Codec detection failed');
  const capabilities=await response.json();
  for(const [select,items] of [[videoCodecSelect,capabilities.video],[audioCodecSelect,capabilities.audio]]){
   if(!select)continue;
   select.querySelectorAll('option[data-detected]').forEach(option=>option.remove());
   for(const item of items){
    const option=document.createElement('option');option.value=item.id;
    option.dataset.detected='1';
    const browserOK=supportsCodec(item.browser_codec);
    option.disabled=!item.available||!browserOK;
    option.textContent=item.label+(!item.available?' · unavailable on server':!browserOK?' · unsupported by browser':item.backend==='software'?' · software':'');
    select.appendChild(option);
   }
   select.value=select===videoCodecSelect?(quality==='audio_only'?'copy':videoCodec):audioCodec;
  }
  codecCapabilities=capabilities;codecHint();
 }catch(_){el('codecStatus').textContent='Codec detection unavailable. Auto playback is still available.';el('retryCodecs').hidden=false;}
 finally{codecLoading=false;if(audioOnly&&quality!=='audio_only'&&quality!=='source'&&codecCapabilities)loadCodecOptions();}
}
function changeCodecs(){
 const wantVideo=quality==='audio_only'?'auto':videoCodecSelect.value,wantAudio=audioCodecSelect?.value||'auto';
 const v=codecCapabilities?.video.find(item=>item.id===(wantVideo==='auto'?'h264':wantVideo));
 const a=codecCapabilities?.audio.find(item=>item.id===(wantAudio==='auto'?'aac':wantAudio));
 const codecString=[...(quality==='audio_only'?[]:[v?.browser_codec]),...(audioSelect?[a?.browser_codec]:[])].filter(Boolean).join(', ');
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
 put('statMethod',streamIsDirect?'Direct play':quality==='audio_only'?'Direct stream · audio conversion':'Transcoding');
 put('statReason',quality==='audio_only'?'Original video copied · only audio is converted':quality==='source'?'Original selected · transcoding disabled':streamIsDirect?'Browser supports the original file':forceTranscode?'Direct playback failed':videoCodec!=='auto'||audioCodec!=='auto'?'Selected video / audio codec':quality!=='auto'?'Selected quality profile':audioSelect&&audioSelect.selectedIndex>0?'Selected audio track':'Source requires conversion');
 put('statVideoCodec',streamIsDirect||quality==='audio_only'?codec(originalVideo):videoCodecNames[lastTechnical?.video_codec||(videoCodec==='auto'?'h264':videoCodec)]);
 put('statAudioCodec',!selectedAudio?'No audio':streamIsDirect?`${codec(selectedAudio)} · ${selectedAudio.channels||'?'} ${selectedAudio.channels===1?'channel':'channels'}`:`${audioCodecNames[lastTechnical?.audio_codec||(audioCodec==='auto'?'aac':audioCodec)]} · stereo · 48 kHz`);
 put('statResolution',video.videoWidth&&video.videoHeight?`${video.videoWidth} × ${video.videoHeight}`:'Waiting for metadata');
 const videoKbps=quality==='custom'?custom.bitrate:profile?.video_kbps;
 put('statVideoTarget',streamIsDirect||quality==='audio_only'?'Original (no conversion)':videoKbps?`${videoKbps/1000} Mbps`:'Variable bitrate (no cap)');
 put('statAudioTarget',!selectedAudio?'No audio':streamIsDirect?'Original':`${quality==='custom'?custom.audio:profile.audio_kbps} Kbps`);
 if(streamIsDirect)put('statEncoder','None · direct delivery');
 put('statToneMapping',streamIsDirect?'Off · original stream':lastTechnical?.tone_mapping||'Waiting for encoder');
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
   if(lastTechnical?.error)put('statReason',lastTechnical.error);
   put('statToneMapping',streamIsDirect?'Off · original stream':lastTechnical?.tone_mapping||'—');
   if(!streamIsDirect){put('statEncoder',lastTechnical?.encoder_label||stream.detail.split(' · ')[0]);if(lastTechnical?.fallback&&!lastTechnical?.error)put('statReason',lastTechnical.fallback);}
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
window.addEventListener('pagehide',()=>{subtitleWindows.stop();cancelPendingSeek();clearInterval(statsTimer);clearControlsTimer();statsRequest?.abort();});
window.addEventListener('pageshow',event=>{if(event.persisted&&started)subtitles(streamIsDirect?0:logicalOffset,logicalTime());});
start(0);updateFullscreenState();
