'use strict';

(function () {
const pendingJavaStarts = new Map();
const libc = Process.getModuleByName('libc.so');
const getTidAddress = libc.findExportByName('pthread_gettid_np');
const getTid = getTidAddress ? new NativeFunction(getTidAddress, 'int', ['pointer']) : null;

function addressInfo(address) {
    const module = Process.findModuleByAddress(address);
    return { address: address.toString(), module: module ? module.name : null,
        offset: module ? address.sub(module.base).toString() : null };
}

function emit(kind, value) {
    console.log('[THREAD-TRACE][' + kind + '] ' + JSON.stringify(Object.assign({
        kind: kind, at: new Date().toISOString(), pid: Process.id
    }, value)));
}

Interceptor.attach(libc.getExportByName('pthread_create'), {
    onEnter(args) {
        this.output = args[0];
        this.info = { creatorTid: Process.getCurrentThreadId(), start: addressInfo(args[2]),
            creator: addressInfo(this.returnAddress), argument: args[3].toString(),
            javaThread: pendingJavaStarts.get(Process.getCurrentThreadId()) || null };
        if (this.info.start.module === 'libnesec.so' || this.info.creator.module === 'libnesec.so') {
            try { this.info.stack = Thread.backtrace(this.context, Backtracer.ACCURATE).map(addressInfo); } catch (_) {}
        }
    },
    onLeave(result) {
        this.info.result = result.toInt32();
        if (this.info.result === 0) {
            try {
                const handle = this.output.readPointer();
                this.info.pthread = handle.toString();
                if (getTid) this.info.createdTid = getTid(handle);
            } catch (error) { this.info.tidError = String(error); }
        }
        emit('PTHREAD_CREATE', this.info);
    }
});

if (false) Java.perform(function () {
    const Thread = Java.use('java.lang.Thread');
    const start = Thread.start.overload();
    start.implementation = function () {
        const info = { name: String(this.getName()), type: String(this.getClass().getName()) };
        try {
            const target = this.target.value;
            info.runnable = target ? String(Java.cast(target, Java.use('java.lang.Object')).getClass().getName()) : null;
        } catch (error) { info.targetError = String(error); }
        try { info.stack = Java.backtrace({ limit: 24 }).frames.map(frame => frame.signature); } catch (_) {}
        const creatorTid = Process.getCurrentThreadId();
        pendingJavaStarts.set(creatorTid, info);
        emit('JAVA_START', info);
        try { return start.call(this); }
        finally { pendingJavaStarts.delete(creatorTid); }
    };
    emit('READY_JAVA', {});
});
emit('READY_NATIVE', {});
})();
