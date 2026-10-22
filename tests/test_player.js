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
for (const name of ['usingDirect', 'updateQualityInfo', 'cancelPendingSeek', 'transcode', 'start']) {
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
