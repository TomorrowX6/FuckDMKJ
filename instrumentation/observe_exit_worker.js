'use strict';

(function () {
// Capture the responsible worker, then reproduce the original process exit.
const libc = Process.getModuleByName('libc.so');
const syscall = new NativeFunction(libc.getExportByName('syscall'), 'long', ['long', '...', 'int']);
const replacements = [];
const installed = new Set();
let output = null;
try { output = new File('/data/user/0/com.jingcai.apps.qualitydev/cache/exit-worker-' + Process.id + '.jsonl', 'a'); } catch (_) {}

function install(module) {
        if (module.name !== 'libnesec.so' || installed.has(module.base.toString())) return;
        const entry = module.base.add(0x2f688);
        if (entry.readU32() !== 0xd2800bc8 || entry.add(4).readU32() !== 0xd4000001) {
            return;
        }
        const replacement = new NativeCallback(function (status) {
            const event = { kind: 'EXIT_WORKER', pid: Process.id, tid: Process.getCurrentThreadId(),
                at: new Date().toISOString(), status: status, entry: entry.toString(),
                caller: this.context.lr.toString() };
            const line = JSON.stringify(event);
            if (output) { output.write(line + '\n'); output.flush(); }
            console.log('[EXIT-WORKER] ' + line);
            return syscall(94, status);
        }, 'long', ['int']);
        replacements.push(replacement);
        Interceptor.replace(entry, replacement);
        installed.add(module.base.toString());
        console.log('[EXIT-WORKER-READY] pid=' + Process.id);
}

const observer = Process.attachModuleObserver({ onAdded: install });
const creationListener = Interceptor.attach(libc.getExportByName('pthread_create'), {
    onEnter(args) {
        const module = Process.findModuleByAddress(args[2]);
        if (module && module.name === 'libnesec.so') install(module);
    }
});
globalThis.__exitWorkerCaptureState = { observer: observer, listener: creationListener, replacements: replacements };
})();
