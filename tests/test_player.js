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
  newRequest() {}, subtitles() {}, codecHint() {}, msg(text) { this.message = text; },
};
// Use an arrow closure so msg works when invoked as a plain script function.
context.msg = text => { context.message = text; };
vm.createContext(context);
for (const name of ['usingDirect', 'updateQualityInfo', 'transcode', 'start']) {
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
