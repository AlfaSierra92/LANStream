"use strict";

// Fetch short overlapping windows; never ask FFmpeg to scan the rest of a film.
// The browser's VTT parser retains cue markup, positioning and line settings.
class WindowedSubtitles {
 constructor(video,url,status,duration=0){
  this.video=video;this.url=url;this.status=status;this.duration=duration;this.stream='';this.base=0;
  this.track=null;this.loaded=new Set();this.keys=new Map();this.request=null;
  this.retryTimer=null;this.retryAfter=0;this.generation=0;
  for(const event of ['timeupdate','seeking','loadedmetadata'])video.addEventListener(event,()=>this.update());
 }
 select(stream,base,at){
  this.stop();this.stream=stream;this.base=base;this.loaded.clear();this.keys.clear();
  if(this.track){for(const cue of Array.from(this.track.cues||[]))this.track.removeCue(cue);this.track.mode='disabled';}
  this.notice('');
  if(!stream)return;
  if(!this.track)this.track=this.video.addTextTrack('subtitles','Subtitles','und');
  this.track.mode='showing';this.update(at);
 }
 notice(text){if(this.status){this.status.textContent=text;this.status.hidden=!text;}}
 stop(){
  this.generation++;this.request?.controller.abort();this.request=null;
  this.stream='';
  clearTimeout(this.retryTimer);this.retryTimer=null;this.retryAfter=0;
 }
 update(at=this.base+(this.video.currentTime||0)){
  if(!this.stream||!Number.isFinite(at)||(this.duration>0&&at>=this.duration))return;
  // Bound browser cue/window memory even over a very long playback session.
  const local=at-this.base;
  for(const [key,cue] of this.keys){if(cue.endTime<local-45||cue.startTime>local+60){this.track.removeCue(cue);this.keys.delete(key);}}
  const current=Math.floor(Math.max(0,at)/15)*15;
  for(const window of this.loaded)if(Math.abs(window-current)>60)this.loaded.delete(window);
  if(this.request){
   // Keep useful overlapping requests on natural progression; abandon a seek
   // far outside their coverage, or a response for a previous media session.
   if(at<this.request.start-15||at>=this.request.start+20){this.request.controller.abort();this.request=null;}
   else return;
  }
  if(Date.now()<this.retryAfter)return;
  if(!this.loaded.has(current))this.load(current);
  else if(at>=current+5&&!this.loaded.has(current+15)&&(!this.duration||current+15<this.duration))this.load(current+15);
 }
 async load(start){
  const job={start,controller:new AbortController(),generation:this.generation};
  this.request=job;
  const timeout=setTimeout(()=>job.controller.abort(),35000);
  try{
   const response=await fetch(this.url+'?'+new URLSearchParams({stream:this.stream,start,duration:20,absolute:1}),
                              {cache:'no-store',signal:job.controller.signal});
   if(!response.ok){const error=new Error('Subtitle request failed');error.status=response.status;throw error;}
   const text=await response.text();
   if(this.request!==job||job.generation!==this.generation)return;
   const cues=await this.parse(text,job.controller.signal);
   if(this.request!==job||job.generation!==this.generation)return;
   for(const cue of cues){
    if(cue.endTime<=this.base)continue;
    const key=[cue.startTime,cue.endTime,cue.text].join('\u0000');
    if(this.keys.has(key))continue;
    const begin=Math.max(0,cue.startTime-this.base),end=cue.endTime-this.base;
    const overlap=Array.from(this.keys.values()).find(old=>old.text===cue.text&&old.align===cue.align&&old.line===cue.line&&old.position===cue.position&&old.startTime<=end&&old.endTime>=begin);
    if(overlap){overlap.startTime=Math.min(overlap.startTime,begin);overlap.endTime=Math.max(overlap.endTime,end);continue;}
    const copy=new VTTCue(Math.max(0,cue.startTime-this.base),cue.endTime-this.base,cue.text);
    for(const property of ['vertical','snapToLines','line','lineAlign','position','positionAlign','size','align']){
     try{copy[property]=cue[property];}catch(_){}
    }
    this.track.addCue(copy);this.keys.set(key,copy);
   }
   this.loaded.add(start);this.retryAfter=0;this.notice('');
  }catch(error){
   if(this.request!==job||job.generation!==this.generation)return;
   if(error.status===415){this.stream='';this.track.mode='disabled';this.notice('This subtitle track cannot be converted to text. Choose another track.');}
   else{
    this.notice('Subtitles are temporarily unavailable; retrying. Video playback can continue.');
    this.retryAfter=Date.now()+5000;
    this.retryTimer=setTimeout(()=>this.update(),5000);
   }
  }finally{
   clearTimeout(timeout);
   if(this.request===job){this.request=null;if(!this.retryAfter)this.update();}
  }
 }
 parse(text,signal){
  return new Promise((resolve,reject)=>{
   const loader=document.createElement('track');
   const url=URL.createObjectURL(new Blob([text],{type:'text/vtt'}));
   let timer;
   const done=(error,cues)=>{
    clearTimeout(timer);signal.removeEventListener('abort',abort);
    loader.onload=loader.onerror=null;loader.remove();URL.revokeObjectURL(url);
    error?reject(error):resolve(cues);
   };
   const abort=()=>done(new DOMException('Cancelled','AbortError'));
   if(signal.aborted){abort();return;}
   signal.addEventListener('abort',abort,{once:true});
   loader.onload=()=>done(null,Array.from(loader.track.cues||[]));
   loader.onerror=()=>done(new Error('Invalid WebVTT'));
   timer=setTimeout(()=>done(new Error('WebVTT parsing timed out')),5000);
   loader.src=url;this.video.appendChild(loader);loader.track.mode='hidden';
  });
 }
}
