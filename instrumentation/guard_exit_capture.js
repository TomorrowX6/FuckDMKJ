'use strict';

// Diagnostic experiment: captures an exit caller, but returning from this
// no-return path was observed to crash its caller. Not used for stable capture.
// This offset and signature are specific to the libnesec.so inspected here.
const targetModule = 'libnesec.so';
const targetOffset = 0x2f688;
const expectedPrologue = 'c80b80d2010000d4'; // mov x8, #94; svc #0
const installed = new Set();
const callbacks = [];
let intercepted = 0;
let diskLog = null;
const diskPath = '/data/user/0/com.jingcai.apps.qualitydev/cache/exit-guard-' + Process.id + '.jsonl';

try { diskLog = new File(diskPath, 'a'); } catch (error) {
    console.log('[EXIT-GUARD][DISK_ERROR] ' + error);
}

function emit(kind, value) {
    const event = Object.assign({ kind: kind, at: new Date().toISOString(), pid: Process.id }, value);
    const line = JSON.stringify(event);
    if (diskLog !== null) {
        try { diskLog.write(line + '\n'); diskLog.flush(); } catch (_) {}
    }
    console.log('[EXIT-GUARD][' + kind + '] ' + line);
}

function describe(address) {
    let pointer = address;
    try { pointer = pointer.strip(); } catch (_) {}
    const module = Process.findModuleByAddress(pointer);
    let symbol = '';
    try { symbol = DebugSymbol.fromAddress(pointer).toString(); } catch (_) {}
    return { address: pointer.toString(), module: module ? module.name : null,
        offset: module ? pointer.sub(module.base).toString() : null, symbol: symbol };
}

function backtrace(context, mode) {
    try { return Thread.backtrace(context, mode).map(describe); }
    catch (error) { return [{ error: String(error) }]; }
}

function install(module) {
    if (module.name !== targetModule || installed.has(module.base.toString())) return;
    if (Process.arch !== 'arm64') throw new Error('This guard requires ARM64');
    const target = module.base.add(targetOffset);
    const signature = Array.from(new Uint8Array(target.readByteArray(8)))
        .map(byte => byte.toString(16).padStart(2, '0')).join('');
    if (signature !== expectedPrologue || target.add(0x14).readU32() !== 0xd65f03c0) {
        emit('SIGNATURE_MISMATCH', { module: module.path, offset: '0x2f688', actual: signature });
        throw new Error('Exit wrapper does not match the inspected build');
    }

    const replacement = new NativeCallback(function (status) {
        intercepted++;
        try {
            const context = this.context;
            const event = { count: intercepted, tid: Process.getCurrentThreadId(),
                syscall: 'exit_group', syscallNumber: 94, status: status,
                androidExitStatus: status & 255, action: 'return 0',
                entry: describe(target), caller: describe(context.lr) };
            if (intercepted <= 8) {
                event.registers = {};
                for (const key of ['pc', 'sp', 'fp', 'lr', ...Array.from({ length: 29 }, (_, index) => 'x' + index)]) {
                    if (context[key] !== undefined) event.registers[key] = context[key].toString();
                }
                event.accurate = backtrace(context, Backtracer.ACCURATE);
                event.fuzzy = backtrace(context, Backtracer.FUZZY);
                try { event.stack = hexdump(context.sp, { length: 0x400, header: true, ansi: false }); }
                catch (error) { event.stackError = String(error); }
            }
            emit('BLOCKED', event);
        } catch (error) {
            emit('CAPTURE_ERROR', { count: intercepted, error: String(error) });
        }
        return 0;
    }, 'long', ['int']);

    Interceptor.replace(target, replacement);
    callbacks.push(replacement);
    installed.add(module.base.toString());
    emit('READY', { module: module.path, base: module.base.toString(),
        entry: target.toString(), offset: '0x2f688', diskLog: diskPath });
}

const observer = Process.attachModuleObserver({ onAdded: install });
rpc.exports = {
    status() { return { pid: Process.id, installed: installed.size, intercepted: intercepted, diskLog: diskPath }; }
};
