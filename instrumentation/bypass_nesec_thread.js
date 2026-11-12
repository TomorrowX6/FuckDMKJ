'use strict';

(function () {
    // Build-specific entry verified by matching pthread_create TID to exit_group.
    // Inspected file SHA256: 91d0df460b8331a8ead782e946f0a55ede23c51ef2d6a11567eb9f4676c8e754
    const workerOffset = '0xb36c8';
    const workerArgument = ptr(2);
    const libc = Process.getModuleByName('libc.so');
    let redirected = 0;
    let skipped = 0;
    let diskLog = null;
    const diskPath = '/data/user/0/com.jingcai.apps.qualitydev/cache/thread-guard-' + Process.id + '.jsonl';
    try { diskLog = new File(diskPath, 'a'); } catch (_) {}

    function emit(kind, fields) {
        const event = Object.assign({ kind: kind, at: new Date().toISOString(), pid: Process.id }, fields);
        const line = JSON.stringify(event);
        if (diskLog) {
            try { diskLog.write(line + '\n'); diskLog.flush(); } catch (_) {}
        }
        console.log('[THREAD-GUARD][' + kind + '] ' + line);
    }

    const emptyWorker = new NativeCallback(function (argument) {
        skipped++;
        emit('WORKER_RETURNED', { count: skipped, tid: Process.getCurrentThreadId(), argument: argument.toString() });
        return ptr(0);
    }, 'pointer', ['pointer']);

    const listener = Interceptor.attach(libc.getExportByName('pthread_create'), {
        onEnter(args) {
            const originalStart = args[2];
            const module = Process.findModuleByAddress(originalStart);
            if (!module || module.name !== 'libnesec.so') return;
            const offset = originalStart.sub(module.base).toString();
            if (offset !== workerOffset || !args[3].equals(workerArgument)) return;
            args[2] = emptyWorker;
            redirected++;
            emit('REDIRECTED', { count: redirected, creatorTid: Process.getCurrentThreadId(),
                originalStart: originalStart.toString(), module: module.path,
                base: module.base.toString(), offset: offset, argument: args[3].toString() });
        }
    });

    globalThis.__nesecThreadGuard = { listener: listener, worker: emptyWorker,
        status: function () { return { pid: Process.id, redirected: redirected, returned: skipped, diskLog: diskPath }; } };
    emit('READY', { targetOffset: workerOffset, targetArgument: '0x2', diskLog: diskPath });
})();
