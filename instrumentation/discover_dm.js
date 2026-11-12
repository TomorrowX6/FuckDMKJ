'use strict';
(function () {
    const seen = new Set();
    function emit(kind, value) {
        console.log('[DM-DISCOVER] ' + JSON.stringify({ kind: kind, pid: Process.id, value: value }));
    }
    function discover() {
        Java.perform(function () {
            const names = Java.enumerateLoadedClassesSync().filter(function (name) {
                return name.startsWith('com.jingcai.apps.qualitydev.') && /network|request|http|sign|rsa|aes|encrypt|utils/i.test(name);
            });
            for (const name of names) {
                if (seen.has(name)) continue;
                seen.add(name);
                try {
                    const type = Java.use(name);
                    const methods = Array.from(type.class.getDeclaredMethods()).map(method => String(method));
                    emit('CLASS', { name: name, methods: methods });
                } catch (error) { emit('CLASS_ERROR', { name: name, error: String(error) }); }
            }
            for (const query of ['*BaseRequest*!*/su', '*!*signToken*/isu', '*qualitydev*!*sign*/isu', '*qualitydev*!*rsa*/isu']) {
                try {
                    const groups = Java.enumerateMethods(query).map(group => ({ loader: String(group.loader), classes: group.classes }));
                    emit('METHOD_QUERY', { query: query, groups: groups });
                } catch (error) { emit('METHOD_ERROR', { query: query, error: String(error) }); }
            }
            emit('PASS_DONE', { classes: seen.size });
        });
    }
    setTimeout(discover, 1000);
    setTimeout(discover, 4000);
    setTimeout(discover, 8000);
})();
