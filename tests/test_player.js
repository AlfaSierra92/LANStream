// Run with node tests/test_player.js. Exercise the actual player routing and error
// handlers with a small DOM substitute, without requiring a media-capable browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/player.js'), 'utf8');
const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, {value: 'auto', textContent: '', disabled: false});
  return elements.get(id);
};
const context = {
  config: {native: false, nativeMime: 'video/mp4', nativeUrl: '/media/0/movie.strm',
           profiles: {source: {height: 0, video_kbps: 0}, auto: {height: 0, video_kbps: 0}}},
  video: {canPlayType: () => '', load() {}, readyState: 1, play: () => Promise.resolve()},
  quality: 'source', videoCodec: 'hevc', audioCodec: 'opus', forceTranscode: true,
  videoCodecSelect: el('videoCodecSelect'), audioCodecSelect: el('audioCodecSelect'),
  audioSelect: {selectedIndex: 1}, metadataSeek: null, custom: {}, streamIsDirect: false,
  playbackId: 'test', started: false, URLSearchParams, el,
  seekTimer: null, pendingSeek: null, setTimeout, clearTimeout,
  newRequest() {}, subtitles() {}, codecHint() {}, msg(text) { this.message = text; },
};
// Use an arrow closure so msg works when invoked as a plain script function.
context.msg = text => { context.message = text; };
vm.createContext(context);
for (const name of ['usingDirect', 'updateQualityInfo', 'cancelPendingSeek', 'requestPlay', 'transcode', 'start']) {
  const singleLine = source.match(new RegExp(`^function ${name}\\(.*\\}\\s*$`, 'm'));
  const multiLine = source.match(new RegExp(`^function ${name}\\([\\s\\S]*?^\\}`, 'm'));
  vm.runInContext(singleLine ? singleLine[0] : multiLine[0], context);
}
// Original must send original media even when compatibility detection says no,
// overriding stale codec/track selections without entering the transcoder.
context.start(0, false);
assert.equal(context.streamIsDirect, true);
assert.equal(context.video.src, '/media/0/movie.strm?playback_id=test');
assert.equal(context.videoCodec, 'auto');
assert.equal(context.audioCodec, 'auto');
assert.equal(context.audioSelect.selectedIndex, 0);
assert.equal(context.forceTranscode, false);
assert.equal(context.videoCodecSelect.disabled, true);
assert.equal(context.audioCodecSelect.disabled, true);
assert.equal(context.audioSelect.disabled, true);
assert.equal(el('playbackMode').textContent, 'Direct play');
const originalUrl = context.video.src;
context.transcode(0, false);
assert.equal(context.video.src, originalUrl);
assert.match(context.message, /Transcoding is disabled/);
const onError = source.match(/^video.addEventListener\('error'.*$/m)[0];
context.video.addEventListener = (_, callback) => { context.onError = callback; };
vm.runInContext(onError, context);
context.switchStream = change => { change(); context.fallback = true; };
context.onError();
assert.equal(context.fallback, undefined);
assert.match(context.message, /Transcoding is disabled/);
// Auto retains compatibility checks, conversion controls and error fallback.
context.quality = 'auto';
context.config.native = true;
context.video.canPlayType = () => 'probably';
context.updateQualityInfo();
assert.equal(context.usingDirect(), true);
assert.equal(context.audioSelect.disabled, false);
context.onError();
assert.equal(context.fallback, true);
assert.equal(context.forceTranscode, true);
assert.equal(context.usingDirect(), false);
// Audio-only always remuxes the source video and never escalates to video
// encoding after a browser error, including when Original is unsupported.
context.config.profiles.audio_only = {height: 0, video_kbps: 0, audio_kbps: 192};
context.config.transcodeUrl = '/transcode/0/movie.strm';
context.totalDuration = 300;
context.audio = () => '2';
context.quality = 'audio_only';
context.start(15, false);
assert.equal(context.streamIsDirect, false);
assert.match(context.video.src, /quality=audio_only/);
assert.match(context.video.src, /audio=2/);
assert.equal(context.videoCodecSelect.disabled, true);
assert.equal(context.videoCodecSelect.value, 'copy');
assert.equal(context.audioCodecSelect.disabled, false);
assert.match(el('playbackMode').textContent, /Original video/);
context.fallback = false;
context.reportPlaybackFailure = () => {context.reported = true;};
context.onError();
assert.equal(context.reported, true);
assert.equal(context.fallback, false);
context.quality = 'auto';
// Leaving Original while settings are already open must refresh the codec menu.
context.qualitySelect = {value: 'auto'};
context.customQuality = {hidden: false};
context.loadCodecOptions = () => { context.codecRefreshes = (context.codecRefreshes || 0) + 1; };
vm.runInContext(source.match(/^qualitySelect.onchange=.*$/m)[0], context);
context.qualitySelect.onchange();
assert.equal(context.codecRefreshes, 1);
assert.equal(context.customQuality.hidden, true);
console.log('Original: unmodified media, no fallback or encoder request; Auto: conversion fallback verified.');

// Seeking within the transcoded buffer must not reopen FFmpeg or the mount.
for (const name of ['logicalTime', 'go']) {
  const singleLine = source.match(new RegExp(`^function ${name}\\(.*\\}\\s*$`, 'm'));
  const multiLine = source.match(new RegExp(`^function ${name}\\([\\s\\S]*?^\\}`, 'm'));
  vm.runInContext(singleLine ? singleLine[0] : multiLine[0], context);
}
const timers = new Map();
let sequence = 0;
context.setTimeout = callback => { timers.set(++sequence, callback); return sequence; };
context.clearTimeout = id => timers.delete(id);
const requests = [];
context.transcode = (at, autoplay) => requests.push({at, autoplay});
context.totalDuration = 300;
context.logicalOffset = 100;
context.started = true;
context.streamIsDirect = false;
context.video.currentTime = 20;
context.video.paused = false;
context.video.buffered = {length: 1, start: () => 0, end: () => 60};
context.seek = {value: 0};
context.currentLabel = {textContent: ''};
context.fmt = String;
context.go(140);
assert.equal(context.video.currentTime, 40);
assert.equal(requests.length, 0);
assert.equal(timers.size, 0);
context.go(80);
assert.equal(context.logicalTime(), 80);
context.go(context.logicalTime() + 30);
assert.equal(context.video.currentTime, 10);
assert.equal(timers.size, 0);
context.go(200);
context.go(context.logicalTime() + 30);
assert.equal(timers.size, 1);
assert.equal(requests.length, 0);
const flushSeek = () => {const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(callback => callback());};
flushSeek();
assert.deepEqual(requests, [{at: 230, autoplay: true}]);
assert.equal(context.pendingSeek, null);
// Paused seeks stay paused; direct playback does not wait for the debounce.
context.video.paused = true;
context.go(290);
flushSeek();
assert.deepEqual(requests[1], {at: 290, autoplay: false});
context.streamIsDirect = true;
context.go(75);
assert.equal(context.video.currentTime, 75);
assert.equal(timers.size, 0);
// Changing subtitles updates tracks on the existing media timeline.
context.subtitleSelect = {};
context.showControls = () => {};
const subtitleStarts = [];
context.subtitles = at => subtitleStarts.push(at);
vm.runInContext(source.match(/^if\(subtitleSelect\)subtitleSelect.onchange=.*$/m)[0], context);
context.subtitleSelect.onchange();
context.streamIsDirect = false;
context.subtitleSelect.onchange();
assert.deepEqual(subtitleStarts, [0, 100]);
assert.equal(requests.length, 2);
console.log('Buffered seeks reuse playback; rapid remote seeks coalesce; pause and subtitle timeline preserved.');

// A failed <video> request must expose the retained, session-scoped server error
// without reopening the movie or turning off tone mapping.
vm.runInContext(source.match(/^async function reportPlaybackFailure\([\s\S]*?^\}/m)[0], context);
(async()=>{
 // Safari must receive play() during the user's click, before metadata exists.
 context.quality='source';context.video.readyState=0;context.video.paused=true;
 context.video.error=null;context.playbackBlocked=false;
 const mediaEvents={};let playCalls=0;
 context.video.addEventListener=(name,callback)=>{mediaEvents[name]=callback;};
 context.video.play=()=>{playCalls++;return Promise.reject({name:'NotAllowedError'});};
 context.start(0);
 assert.equal(playCalls,1);
 await Promise.resolve();
 assert.equal(context.playbackBlocked,true);
 assert.match(context.message,/Tap ▶/);
 for(const name of ['waiting','loadeddata']){
  vm.runInContext(source.match(new RegExp(`^video.addEventListener\\('${name}'.*$`,'m'))[0],context);
  mediaEvents[name]();
  assert.match(context.message,/Tap ▶/);
 }
 context.video.currentTime=7;
 mediaEvents.loadedmetadata();
 assert.equal(playCalls,1);
 assert.equal(context.video.currentTime,7); // No redundant seek to zero.
 context.video.play=()=>{playCalls++;return Promise.resolve();};
 context.requestPlay();
 assert.equal(playCalls,2);assert.equal(context.playbackBlocked,false);
 // Ignore an autoplay rejection belonging to an obsolete media request.
 context.video.play=()=>Promise.reject({name:'NotAllowedError'});
 context.requestPlay();context.playbackId='replacement';context.message='new playback';
 await Promise.resolve();
 assert.equal(context.message,'new playback');
 console.log('Safari: play starts before metadata; blocked autoplay prompts for a tap; stale failures ignored.');
 context.playbackId='hdr-error';context.streamIsDirect=false;context.video.error={code:3};
 context.config.statsUrl='/api/playback-info/0/movie.mkv';context.showControls=()=>{};
 context.put=(id,value)=>{el(id).textContent=value;};
 const urls=[];
 context.fetch=async url=>{urls.push(url);return {ok:true,json:async()=>({stream:{technical:{error:'VAAPI driver does not support HDR'}}})};};
 await context.reportPlaybackFailure();
 assert.match(context.message,/VAAPI driver does not support HDR/);
 assert.equal(el('statReason').textContent,'VAAPI driver does not support HDR');
 assert.equal(urls.length,1);assert.match(urls[0],/playback_id=hdr-error/);
 context.fetch=async()=>({ok:true,json:async()=>{context.playbackId='new-video';return {stream:{technical:{error:'stale error'}}};}});
 context.message='new playback';
 await context.reportPlaybackFailure();
 assert.doesNotMatch(context.message,/stale error/);
 console.log('Playback failures show the retained server reason; stale replies cannot overwrite a new request.');
})().catch(error=>{console.error(error);process.exitCode=1;});
