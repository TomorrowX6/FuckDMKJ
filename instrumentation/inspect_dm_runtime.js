'use strict';
(function () {
    function emit(kind, value) {
        console.log('[DM-RUNTIME] ' + JSON.stringify({ kind: kind, pid: Process.id, value: value }));
    }
    function inspect() {
        Java.perform(function () {
            const names = ['k3.t', 'x2.f', 'yb.w', 'yb.y', 'yb.y$a',
                'com.jingcai.apps.qualitydev.network.netRequesthttp.BaseRequest',
                'com.jingcai.apps.qualitydev.utils.NativeUtil'];
            for (const name of names) {
                try {
                    const type = Java.use(name);
                    emit('CLASS', { name: name,
                        methods: Array.from(type.class.getDeclaredMethods()).map(method => String(method)),
                        fields: Array.from(type.class.getDeclaredFields()).map(field => String(field)) });
                } catch (error) { emit('CLASS_ERROR', { name: name, error: String(error) }); }
            }
            try {
                const app = Java.use('android.app.ActivityThread').currentApplication();
                const context = app.getApplicationContext();
                const NativeUtil = Java.use('com.jingcai.apps.qualitydev.utils.NativeUtil');
                for (const method of ['getNetSignExponent', 'getNetSignModuleKey', 'getNetSignPlIV', 'getNetSignReIV']) {
                    emit('NATIVE_CONSTANT', { method: method, value: String(NativeUtil[method](context)) });
                }
            } catch (error) { emit('CONSTANT_ERROR', String(error)); }
            try {
                const target = Java.use('com.jingcai.apps.qualitydev.network.netRequesthttp.BaseRequest');
                const loader = target.class.getClassLoader();
                emit('CLASS_LOADER', String(loader));
                const baseLoader = Java.cast(loader, Java.use('dalvik.system.BaseDexClassLoader'));
                const elements = baseLoader.pathList.value.dexElements.value;
                const dumped = new Set();
                const ReflectArray = Java.use('java.lang.reflect.Array');
                for (let elementIndex = 0; elementIndex < elements.length; elementIndex++) {
                    const dexFile = elements[elementIndex].dexFile.value;
                    if (dexFile === null) continue;
                    const cookie = dexFile.mCookie.value;
                    const cookieLength = ReflectArray.getLength(cookie);
                    emit('DEX_COOKIE', { name: String(dexFile), entries: cookieLength });
                    for (let cookieIndex = 1; cookieIndex < cookieLength; cookieIndex++) {
                        const object = ptr('0x' + uint64(String(ReflectArray.getLong(cookie, cookieIndex))).toString(16));
                        if (object.isNull()) continue;
                        for (let offset = 0; offset < 0x60; offset += Process.pointerSize) {
                            try {
                                const begin = object.add(offset).readPointer();
                                if (begin.isNull() || dumped.has(begin.toString())) continue;
                                const magic = new Uint8Array(begin.readByteArray(8));
                                if (magic[0] !== 0x64 || magic[1] !== 0x65 || magic[2] !== 0x78 || magic[3] !== 0x0a) continue;
                                const size = begin.add(0x20).readU32();
                                if (size < 112 || size > 80 * 1024 * 1024) continue;
                                dumped.add(begin.toString());
                                const matches = Memory.scanSync(begin, size, '42 61 73 65 52 65 71 75 65 73 74');
                                emit('DEX_CANDIDATE', { begin: begin.toString(), size: size, containsBaseRequest: matches.length > 0 });
                                if (matches.length === 0) continue;
                                const path = '/data/user/0/com.jingcai.apps.qualitydev/cache/dm-business-' + Process.id + '-' + dumped.size + '.dex';
                                const file = new File(path, 'wb');
                                file.write(begin.readByteArray(size));
                                file.flush();
                                file.close();
                                emit('DEX_DUMP', { path: path, size: size });
                            } catch (_) {}
                        }
                    }
                }
            } catch (error) { emit('DEX_ERROR', String(error)); }
            emit('DONE', {});
        });
    }
    setTimeout(inspect, 3000);
})();
