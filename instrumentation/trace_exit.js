'use strict';

// Observe termination without changing return values or suppressing exits.
const installed = [];
const libc = Process.getModuleByName('libc.so');
const diskLogPath = '/data/user/0/com.jingcai.apps.qualitydev/cache/exittrace-' + Process.id + '.log';
let diskLog = null;
try { diskLog = new File(diskLogPath, 'w'); } catch (error) {
    console.log('[EXITTRACE][DISK_ERROR] ' + error);
}

function emit(text) {
    if (diskLog !== null) {
        try { diskLog.write(text + '\n'); diskLog.flush(); } catch (_) {}
    }
    console.log(text);
}

function describeAddress(address) {
    const module = Process.findModuleByAddress(address);
    let symbol = '';
    try { symbol = DebugSymbol.fromAddress(address).toString(); } catch (_) {}
    return address + ' ' + (module ? module.name + '+' + address.sub(module.base) + ' [' + module.path + ']' : '<unknown module>') + ' ' + symbol;
}

function nativeTrace(context, returnAddress) {
    let frames = [];
    let mode = 'ACCURATE';
    try { frames = Thread.backtrace(context, Backtracer.ACCURATE); } catch (_) {}
    if (frames.length < 2) {
        mode = 'FUZZY';
        try { frames = Thread.backtrace(context, Backtracer.FUZZY); } catch (_) {}
    }
    return 'caller: ' + describeAddress(returnAddress) + '\n' + mode + '\n' + frames.map(describeAddress).join('\n');
}

function recordNative(name, details, invocation) {
    emit('\n[EXITTRACE][NATIVE] ' + name + ' ' + details +
        ' pid=' + Process.id + ' tid=' + Process.getCurrentThreadId() + ' at=' + new Date().toISOString() +
        '\n' + nativeTrace(invocation.context, invocation.returnAddress) + '\n[/EXITTRACE]');
}

function attachExport(name, details, shouldRecord) {
    const address = libc.findExportByName(name);
    if (address === null) return;
    Interceptor.attach(address, {
        onEnter(args) {
            if (shouldRecord && !shouldRecord(args)) return;
            recordNative(name, details(args), this);
        }
    });
    installed.push(name);
}

['exit', '_exit', '_Exit', 'quick_exit', 'exit_group'].forEach(function (name) {
    attachExport(name, args => 'status=' + args[0].toInt32());
});
attachExport('abort', () => '');
attachExport('raise', args => 'signal=' + args[0].toInt32());
attachExport('kill', args => 'targetPid=' + args[0].toInt32() + ' signal=' + args[1].toInt32(), args => args[1].toInt32() !== 0);
attachExport('tkill', args => 'targetTid=' + args[0].toInt32() + ' signal=' + args[1].toInt32(), args => args[1].toInt32() !== 0);
attachExport('tgkill', args => 'targetPid=' + args[0].toInt32() + ' targetTid=' + args[1].toInt32() + ' signal=' + args[2].toInt32(), args => args[2].toInt32() !== 0);
attachExport('pthread_kill', args => 'thread=' + args[0] + ' signal=' + args[1].toInt32(), args => args[1].toInt32() !== 0);

if (Process.arch === 'arm64') {
    attachExport('syscall', args => 'nr=' + args[0].toInt32() + ' a1=' + args[1] + ' a2=' + args[2] + ' a3=' + args[3], function (args) {
        const number = args[0].toInt32();
        if (number === 93 || number === 94) return true;
        if (number === 129 || number === 130) return args[2].toInt32() !== 0;
        if (number === 131) return args[3].toInt32() !== 0;
        return false;
    });
}

emit('[EXITTRACE][READY][NATIVE] pid=' + Process.id + ' arch=' + Process.arch + ' exports=' + installed.join(',') + ' disk=' + diskLogPath);

Java.perform(function () {
    const targets = [
        ['java.lang.System', 'exit', ['int']],
        ['java.lang.Runtime', 'exit', ['int']],
        ['java.lang.Runtime', 'halt', ['int']],
        ['android.os.Process', 'killProcess', ['int']],
        ['android.os.Process', 'sendSignal', ['int', 'int']],
        ['android.os.Process', 'sendSignalQuiet', ['int', 'int']]
    ];
    targets.forEach(function (target) {
        const className = target[0];
        const methodName = target[1];
        try {
            const type = Java.use(className);
            const method = type[methodName].overload.apply(type[methodName], target[2]);
            method.implementation = function () {
                const args = Array.prototype.slice.call(arguments);
                let stack = '';
                try {
                    stack = Java.backtrace({ limit: 40 }).frames.map(function (frame) {
                        return frame.signature + ' [' + frame.origin + ']';
                    }).join('\n');
                } catch (error) { stack = 'Java backtrace unavailable: ' + error; }
                emit('\n[EXITTRACE][JAVA] ' + className + '.' + methodName + '(' + args.join(', ') + ')' +
                    ' pid=' + Process.id + ' tid=' + Process.getCurrentThreadId() + ' at=' + new Date().toISOString() +
                    '\n' + stack + '\n[/EXITTRACE]');
                return method.apply(this, args);
            };
            emit('[EXITTRACE][HOOKED] ' + className + '.' + methodName);
        } catch (error) {
            emit('[EXITTRACE][SKIP] ' + className + '.' + methodName + ': ' + error);
        }
    });
    emit('[EXITTRACE][READY][JAVA]');
});
