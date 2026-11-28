/* Stockfish 19 lite, single-threaded browser worker.
   Try independent CDNs so one blocked CDN does not stop the bot from loading.
   Each CDN also serves the matching WebAssembly file. */
self.Module = self.Module || {};

const stockfishBases = [
    "https://unpkg.com/stockfish@19.0.1/bin/",
    "https://cdn.jsdelivr.net/npm/stockfish@19.0.1/bin/"
];

let stockfishLoaded = false;
const stockfishErrors = [];

for (const base of stockfishBases) {
    try {
        self.Module.locateFile = function (path) {
            return base + path;
        };
        importScripts(base + "stockfish-19-lite-single.js");
        stockfishLoaded = true;
        break;
    } catch (error) {
        stockfishErrors.push(base + " — " + String(error));
    }
}

if (!stockfishLoaded) {
    throw new Error("Could not load Stockfish from any available CDN. " + stockfishErrors.join(" | "));
}
