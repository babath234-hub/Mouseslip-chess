/* Stockfish 19 lite, single-threaded browser worker.
   The matching WebAssembly file is loaded from the same pinned jsDelivr package version. */
self.Module = self.Module || {};
self.Module.locateFile = function (path) {
    return "https://cdn.jsdelivr.net/npm/stockfish@19.0.1/bin/" + path;
};
importScripts("https://cdn.jsdelivr.net/npm/stockfish@19.0.1/bin/stockfish-19-lite-single.js");
