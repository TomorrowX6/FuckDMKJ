/*
 * DM protocol probe for the inspected 5.0.0 build, Frida 17.
 * Load bypass_nesec_thread.js before this file when spawning the app.
 * Emits JSON lines. Captures include account data; keep the output local.
 */
'use strict';
(function () {
  const hooks = new Set();
  const warnings = new Set();
  const requests = new Map();
  const stages = new Map();
  let sequence = 0;
  let ready = false;
  let constants = null;

  function top(map) {
    const values = map.get(Process.getCurrentThreadId());
    return values && values.length ? values[values.length - 1] : null;
  }
  function within(map, value, action) {
    const tid = Process.getCurrentThreadId();
    const values = map.get(tid) || [];
    values.push(value);
    map.set(tid, values);
    try { return action(); }
    finally { values.pop(); if (!values.length) map.delete(tid); }
  }
  function emit(kind, value) {
    const request = top(requests);
    console.log('[DM-PROBE] ' + JSON.stringify(Object.assign({ kind: kind,
      at: new Date().toISOString(), pid: Process.id, tid: Process.getCurrentThreadId(),
      requestId: request ? request.id : null, url: request ? request.url : null
    }, value)));
  }
  function mapObject(value) {
    if (value === null) return null;
    const map = Java.cast(value, Java.use('java.util.Map'));
    const iterator = map.entrySet().iterator();
    const result = Object.create(null);
    while (iterator.hasNext()) {
      const entry = Java.cast(iterator.next(), Java.use('java.util.Map$Entry'));
      const item = entry.getValue();
      result[String(entry.getKey())] = item === null ? null : String(item);
    }
    return result;
  }
  function stack() {
    try { return Java.backtrace({ limit: 16 }).frames.map(frame => frame.signature); }
    catch (_) { return []; }
  }
  function hook(className, methodName, handler) {
    const name = className + '.' + methodName;
    if (hooks.has(name)) return;
    const type = Java.use(className);
    const overloads = type[methodName].overloads;
    for (const method of overloads) {
      method.implementation = function () {
        return handler.call(this, method, Array.prototype.slice.call(arguments));
      };
    }
    hooks.add(name);
    emit('HOOKED', { method: name, overloads: overloads.length });
  }
  function attempt() {
    Java.perform(function () {
      const installs = [
        function () { hook('com.jingcai.apps.qualitydev.network.netRequesthttp.BaseRequest', 'post', function (method, args) {
          const receiver = this;
          const context = { id: ++sequence, url: String(args[0]) };
          return within(requests, context, function () {
            emit('REQUEST_PARAMS', { params: mapObject(args[1]) });
            const result = method.apply(receiver, args);
            emit('SIGNED_PARAMS', { params: mapObject(args[1]) });
            return result;
          });
        }); },
        function () { hook('x2.f', 'd', function (method, args) {
          const receiver = this;
          const params = mapObject(args[0]);
          return within(stages, 'sign', function () {
            const result = method.apply(receiver, args);
            emit('SIGN_TOKEN', { params: params, signToken: String(result), stack: stack() });
            return result;
          });
        }); },
        function () { hook('k3.t', 'd', function (method, args) {
          const result = method.apply(this, args);
          if (top(stages) === 'sign') emit('SIGN_MD5_INPUT', { input: String(args[0]), result: String(result) });
          return result;
        }); },
        function () { hook('a3.b', 'e', function (method, args) {
          const result = method.apply(this, args);
          const stage = top(stages);
          if (stage) emit('SERIALIZED', { stage: stage, json: String(result) });
          return result;
        }); },
        function () { hook('x2.d', 'a', function (method, args) {
          const receiver = this;
          return within(stages, 'transport', function () {
            const result = method.apply(receiver, args);
            emit('TRANSPORT_FIELDS', { fields: mapObject(result) });
            return result;
          });
        }); },
        function () { hook('x2.a', 'c', function (method, args) {
          const result = method.apply(this, args);
          const key = String(result);
          emit('AES_KEY_GENERATED', { method: 'x2.a.c()', key: key, length: key.length, stack: stack() });
          return result;
        }); },
        function () { hook('x2.a', 'b', function (method, args) {
          const result = method.apply(this, args);
          if (top(stages)) emit('AES_ENCRYPT', { plaintext: String(args[0]), key: String(args[1]), iv: String(args[2]), ciphertext: String(result) });
          return result;
        }); },
        function () { hook('x2.a', 'a', function (method, args) {
          const result = method.apply(this, args);
          emit('AES_DECRYPT', { ciphertext: String(args[0]), key: String(args[1]), iv: String(args[2]), plaintext: result === null ? null : String(result) });
          return result;
        }); },
        function () { hook('x2.e', 'a', function (method, args) {
          const result = method.apply(this, args);
          if (top(stages) === 'transport') emit('RSA_WRAP', { key: String(args[1]), ciphertext: String(result), length: String(result).length });
          return result;
        }); },
        function () { hook('x2.e', 'b', function (method, args) {
          const result = method.apply(this, args);
          const value = { modulus: String(args[0]), exponent: String(args[1]) };
          if (result !== null) {
            const key = Java.cast(result, Java.use('java.security.Key'));
            value.derBase64 = String(Java.use('android.util.Base64').encodeToString(key.getEncoded(), 2));
          }
          emit('RSA_PUBLIC_KEY', value);
          return result;
        }); },
        function () { hook('com.jingcai.apps.qualitydev.network.netRequesthttp.BaseRequest', 'getHeader', function (method, args) {
          const result = method.apply(this, args);
          emit('HTTP_HEADER', { name: 'standardUA', value: String(result) });
          return result;
        }); },
        function () { hook('yb.y$a', 'b', function (method, args) {
          const result = method.apply(this, args);
          try {
            const url = String(result.j());
            if (url.indexOf('5idream.net') >= 0) {
              const body = result.a();
              const fields = [];
              if (body !== null) {
                const type = Java.cast(body, Java.use('java.lang.Object')).getClass();
                const List = Java.use('java.util.List');
                for (const field of Array.from(type.getDeclaredFields())) {
                  if (Java.use('java.lang.reflect.Modifier').isStatic(field.getModifiers())) continue;
                  field.setAccessible(true);
                  const value = field.get(body);
                  if (value === null || !List.class.isInstance(value)) continue;
                  const list = Java.cast(value, List);
                  const items = [];
                  for (let index = 0; index < list.size(); index++) items.push(String(list.get(index)));
                  fields.push({ name: String(field.getName()), values: items });
                }
              }
              emit('HTTP_REQUEST', { url: url, method: String(result.f()), headers: String(result.d()), bodyFields: fields });
            }
          } catch (error) { emit('HTTP_CAPTURE_ERROR', { error: String(error) }); }
          return result;
        }); }
      ];
      for (let index = 0; index < installs.length; index++) {
        try { installs[index](); }
        catch (error) {
          if (!warnings.has(index)) { warnings.add(index); emit('WAITING_FOR_CLASS', { installation: index, error: String(error) }); }
        }
      }
      if (!constants) {
        try {
          const context = Java.use('android.app.ActivityThread').currentApplication().getApplicationContext();
          const native = Java.use('com.jingcai.apps.qualitydev.utils.NativeUtil');
          const adapterField = Java.use('a3.b').class.getDeclaredField('a');
          adapterField.setAccessible(true);
          const jsonAdapter = Java.cast(adapterField.get(null), Java.use('java.lang.Object'));
          constants = { modulus: String(native.getNetSignModuleKey(context)), exponent: String(native.getNetSignExponent(context)),
            requestIv: String(native.getNetSignReIV(context)), responseIv: String(native.getNetSignPlIV(context)),
            formField: String(context.getResources().getString(2131755252)), jsonAdapter: String(jsonAdapter.getClass().getName()) };
          emit('PROTOCOL_CONSTANTS', constants);
        } catch (error) {
          if (!warnings.has('constants')) { warnings.add('constants'); emit('WAITING_FOR_CONSTANTS', { error: String(error) }); }
        }
      }
      if (!ready && hooks.size === installs.length && constants) {
        ready = true;
        emit('READY', { methods: hooks.size });
        clearInterval(timer);
      }
    });
  }

  function reference(params) {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const map = Java.use('java.util.HashMap').$new();
          const tree = Java.use('java.util.TreeMap').$new();
          for (const key of Object.keys(params)) {
            const value = params[key] === null ? null : String(params[key]);
            map.put(key, value);
            tree.put(key, value === null ? '' : value);
          }
          const serialized = String(Java.use('a3.b').e(tree));
          const signToken = String(Java.use('x2.f').d(map));
          const key = 'abcdefghijklmnop';
          const ciphertext = String(Java.use('x2.a').b(serialized, key, constants.requestIv));
          const result = { params: params, serialized: serialized, signToken: signToken,
            aesKey: key, requestIv: constants.requestIv, aesCiphertext: ciphertext };
          emit('ORACLE', result);
          resolve(result);
        } catch (error) { reject(error); }
      });
    });
  }
  globalThis.__dmProbe = { reference: reference, status: function () { return { ready: ready, methods: Array.from(hooks), constants: constants }; } };
  const timer = setInterval(attempt, 500);
  attempt();
})();
