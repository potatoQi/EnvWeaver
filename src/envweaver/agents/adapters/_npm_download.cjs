// Installed only in preparation commands; npm still resolves and installs packages itself.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const Module = require('module');
const load = Module._load;
const method = Symbol.for('pacote.Fetcher._tarballFromResolved');
const patched = new WeakSet();

Module._load = function (name, parent, main) {
  const exported = load.apply(this, arguments);
  const root = process.env.ENVWEAVER_DOWNLOAD_REQUESTS;
  if (!root || typeof exported !== 'function' || !exported.prototype ||
      !Object.hasOwn(exported.prototype, method) || patched.has(exported)) return exported;
  const filename = Module._resolveFilename(name, parent);
  if (!filename.replace(/\\/g, '/').endsWith('/pacote/lib/remote.js')) return exported;
  patched.add(exported);
  const original = exported.prototype[method];
  const imported = Module.createRequire(filename)('minipass');
  const Minipass = imported.Minipass || imported;
  exported.prototype[method] = function () {
    const match = String(this.integrity || '').match(/(?:^|\s)(sha512|sha256)-([A-Za-z0-9+/=]+)(?:\s|$)/);
    if (!match || new URL(this.resolved).username) return original.apply(this, arguments);
    const directory = path.join(root, crypto.randomBytes(16).toString('hex'));
    fs.mkdirSync(directory);
    fs.writeFileSync(path.join(directory, 'request.tmp'), JSON.stringify({
      url: this.resolved,
      checksum: match[1] + ':' + Buffer.from(match[2], 'base64').toString('hex'),
    }));
    fs.renameSync(path.join(directory, 'request.tmp'), path.join(directory, 'request.json'));
    const stream = new Minipass();
    const deadline = Date.now() + 3600000;
    const timer = setInterval(() => {
      try {
        const response = path.join(directory, 'response.json');
        if (!fs.existsSync(response)) {
          if (!fs.existsSync(directory)) throw new Error('Preparation download service stopped');
          if (Date.now() > deadline) throw new Error('Preparation download timed out');
          return;
        }
        clearInterval(timer);
        const result = JSON.parse(fs.readFileSync(response, 'utf8'));
        if (result.native) {
          fs.rmSync(directory, { recursive: true, force: true });
          const input = original.call(this);
          input.on('error', error => stream.emit('error', error));
          input.pipe(stream);
          return;
        }
        if (result.error) throw new Error(result.error);
        const input = fs.createReadStream(path.join(directory, 'body'));
        input.on('error', error => stream.emit('error', error));
        input.on('close', () => fs.rmSync(directory, { recursive: true, force: true }));
        input.pipe(stream);
      } catch (error) {
        clearInterval(timer);
        fs.rmSync(directory, { recursive: true, force: true });
        stream.emit('error', error);
      }
    }, 50);
    return stream;
  };
  return exported;
};
