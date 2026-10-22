// Run with node tests/test_subtitles.js. Exercise the actual window scheduler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/subtitles.js'), 'utf8');
const requests = [], timers = new Map();
let timerId = 0, now = 0;
class Cue {
 constructor(startTime,endTime,text){Object.assign(this,{startTime,endTime,text,align:'center',line:'auto',position:'auto'});}
}
const context = {
 AbortController, URLSearchParams, VTTCue: Cue,
 Date: {now: () => now},
 setTimeout(callback,delay){const id=++timerId;timers.set(id,{callback,delay});return id;},
 clearTimeout(id){timers.delete(id);},
 fetch(url,options){return new Promise(resolve=>requests.push({url,options,resolve}));},
};
vm.createContext(context);
vm.runInContext(source+'\nglobalThis.Controller = WindowedSubtitles;',context);
const track = {mode:'disabled',cues:[],addCue(cue){this.cues.push(cue);},removeCue(cue){this.cues.splice(this.cues.indexOf(cue),1);}};
const video = {currentTime:59,addEventListener(){},addTextTrack(){return track;}};
const status = {};
const manager = new context.Controller(video,'/subtitle/0/movie.mkv',status);
manager.parse = async text => JSON.parse(text).map(cue=>new Cue(...cue));
const tick = () => new Promise(resolve=>setImmediate(resolve));
const complete = async (job,cues=[],code=200) => {
 job.resolve({ok:code===200,status:code,text:async()=>JSON.stringify(cues)});
 await tick();await tick();
};
(async()=>{
 manager.select('3',500,559);
 assert.equal(requests.length,1);
 let params = new URL(requests[0].url,'http://localhost').searchParams;
 assert.equal(params.get('start'),'555');
 assert.equal(params.get('duration'),'20');
 assert.equal(params.get('absolute'),'1');
 await complete(requests[0],[[560,563,'Hello']]);
 assert.equal(track.cues[0].startTime,60);
 assert.equal(track.cues[0].endTime,63);
 // Prefetch before the current window expires; do not abort a future window.
 video.currentTime=60;manager.update();
 assert.equal(requests.length,2);
 params = new URL(requests[1].url,'http://localhost').searchParams;
 assert.equal(params.get('start'),'570');
 video.currentTime=62;manager.update();
 assert.equal(requests.length,2);
 assert.equal(requests[1].options.signal.aborted,false);
 await complete(requests[1],[[571,573,'Next']]);
 assert.equal(track.cues.length,2);
 // Jump while a request is in flight, then complete its stale response.
 video.currentTime=75;manager.update();
 const stale=requests[2];
 video.currentTime=450;manager.update();
 assert.equal(stale.options.signal.aborted,true);
 assert.equal(requests.length,4);
 await complete(stale,[[590,591,'Stale']]);
 assert.equal(track.cues.some(cue=>cue.text==='Stale'),false);
 await complete(requests[3],[[950,952,'Seek cue']]);
 assert.equal(track.cues.find(cue=>cue.text==='Seek cue').startTime,450);
 assert.equal(track.cues.some(cue=>cue.text==='Hello'),false);
 // Turning off subtitles cancels outstanding work and removes old cues.
 manager.select('',500,950);manager.update();
 assert.equal(track.mode,'disabled');assert.equal(track.cues.length,0);
 const count=requests.length;
 manager.stop();manager.update();assert.equal(requests.length,count);
 // A backend timeout leaves video alone and retries after backoff.
 video.currentTime=0;manager.select('3',0,0);
 await complete(requests.at(-1),[],504);
 assert.match(status.textContent,/retrying/);
 const failures=requests.length;
 manager.update();assert.equal(requests.length,failures);
 now=5000;
 const retry=[...timers.values()].find(timer=>timer.delay===5000);
 assert.ok(retry);retry.callback();
 assert.equal(requests.length,failures+1);
 await complete(requests.at(-1),[[1,2,'Recovered']]);
 assert.equal(status.hidden,true);
 assert.equal(track.cues[0].text,'Recovered');
 manager.stop();
 console.log('Subtitle windows: offsets, prefetch, seek cancellation, stale results, cue cleanup and timeout retries verified.');
})().catch(error=>{console.error(error);process.exitCode=1;});
