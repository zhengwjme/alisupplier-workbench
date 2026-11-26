// AliSupplier macOS (arm64) 轻量只读联动脚本：仅监听低频会话切换与按需资料读取
const mod = Process.getModuleByName("libAppBiz.dylib");

const syms = {
  chatOpenAfter: "_ZN12CChatService21OpenConversationAfterERKN4base8LocationERK15AppConversationRK17EChatActionParams",
  getProfileCache: "_ZN18CAppProfileService25GetPersonProfileFromCacheERKN10messagesdk6TargetE"
};

function emit(obj) {
  console.log("__ALISUPPLIER_EVT__" + JSON.stringify(obj));
}

function readArm64StdString(p) {
  try {
    if (!p || p.isNull()) return null;
    const b23 = p.add(23).readU8();
    if ((b23 & 0x80) === 0) {
      const len = b23;
      if (len === 0) return "";
      if (len > 22) return null;
      return p.readUtf8String(len);
    }
    const dataPtr = p.readPointer();
    const len = p.add(8).readU64().toNumber();
    if (len === 0) return "";
    if (dataPtr.isNull() || len > 8192) return null;
    return dataPtr.readUtf8String(len);
  } catch (e) {
    return null;
  }
}

function readExtraMap(mapPtr) {
  const extra = {};
  try {
    if (!mapPtr || mapPtr.isNull()) return extra;
    let node = mapPtr.add(0x10).readPointer();
    let guard = 0;
    while (!node.isNull() && guard < 100) {
      guard++;
      const key = readArm64StdString(node.add(0x10));
      if (key) {
        const valPtr = node.add(0x28);
        const flags = valPtr.add(0x14).readU32();
        const type = flags & 0xFF;
        let val = null;
        if (type === 5 || type === 6) {
          try {
            const strPtr = valPtr.readPointer();
            if (!strPtr.isNull()) val = strPtr.readUtf8String();
          } catch (_) {
            try { val = valPtr.readUtf8String(); } catch (_) {}
          }
        } else {
          const asStd = readArm64StdString(valPtr);
          if (asStd !== null) val = asStd;
        }
        if (val !== null && val !== "") extra[key] = val;
      }
      node = node.readPointer();
    }
  } catch (_) {}
  return extra;
}

function buildUserId(selfUid, cid) {
  if (!selfUid || !cid) return null;
  const atIdx = cid.lastIndexOf("@");
  const suffix = atIdx !== -1 ? cid.slice(atIdx + 1).trim() : "icbu";
  return selfUid + "@" + (suffix || "icbu");
}

const chatOpenAfterAddr = mod.findExportByName(syms.chatOpenAfter);
const getProfileCacheAddr = mod.findExportByName(syms.getProfileCache);

const profileCache = new Map();
let pending = null;
let flushTimer = null;

function flushPending(profile) {
  if (flushTimer) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  if (!pending) return;
  const cached = profile || (pending.uid ? profileCache.get(pending.uid) : null);
  const payload = Object.assign({ event: "active_chat" }, pending, cached || {});
  pending = null;
  emit(payload);
}

if (chatOpenAfterAddr) {
  Interceptor.attach(chatOpenAfterAddr, {
    onEnter(args) {
      const conv = args[2];
      const uid = readArm64StdString(conv.add(0x20));
      const cid = readArm64StdString(conv.add(0xc8));
      const selfUid = readArm64StdString(conv.add(0x258));
      const user_id = buildUserId(selfUid, cid);
      if (!cid || !user_id) return;

      if (pending) flushPending(null);
      pending = {
        user_id,
        cid,
        uid,
        ts: Date.now()
      };
      const cached = uid ? profileCache.get(uid) : null;
      if (cached && cached.display_name) {
        flushPending(cached);
      } else {
        flushTimer = setTimeout(() => flushPending(null), 180);
      }
    }
  });
}

if (getProfileCacheAddr) {
  Interceptor.attach(getProfileCacheAddr, {
    onEnter(args) {
      this.shouldCapture = false;
      const u1 = readArm64StdString(args[1].add(0x8));
      const u2 = readArm64StdString(args[1].add(0x10));
      const uid = (u1 && /^\d+$/.test(u1)) ? u1 : ((u2 && /^\d+$/.test(u2)) ? u2 : null);
      if (!uid) return;
      const needPending = Boolean(pending && pending.uid === uid);
      if (!needPending && profileCache.has(uid)) return;
      this.shouldCapture = true;
      this.outPtr = this.context.x8;
      this.uid = uid;
    },
    onLeave() {
      if (!this.shouldCapture || !this.outPtr) return;
      try {
        const profilePtr = this.outPtr.readPointer();
        if (profilePtr.isNull()) return;
        const display_name = readArm64StdString(profilePtr.add(0x48));
        const avatar_url = readArm64StdString(profilePtr.add(0x150));
        const extra = readExtraMap(profilePtr.add(0x178));
        const info = { display_name, avatar_url, extra };
        if (display_name) {
          if (profileCache.size > 500) profileCache.clear();
          profileCache.set(this.uid, info);
        }
        if (pending && pending.uid === this.uid) {
          flushPending(info);
        }
      } catch (_) {}
    }
  });
}

emit({ event: "ready", pid: Process.id });
