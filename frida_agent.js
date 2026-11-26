// AliSupplier 跨平台（macOS arm64 / Windows x64）轻量只读联动脚本
// 仅监听低频会话切换与按需资料读取，避免列表渲染高频卡顿

function emit(obj) {
  console.log("__ALISUPPLIER_EVT__" + JSON.stringify(obj));
}

function buildUserId(selfUid, cid) {
  if (!selfUid || !cid) return null;
  const atIdx = cid.lastIndexOf("@");
  const suffix = atIdx !== -1 ? cid.slice(atIdx + 1).trim() : "icbu";
  return selfUid + "@" + (suffix || "icbu");
}

// ---------------------------------------------------------------- macOS (arm64 libc++)
function setupDarwin() {
  const mod = Process.getModuleByName("libAppBiz.dylib");
  const syms = {
    chatOpenAfter: "_ZN12CChatService21OpenConversationAfterERKN4base8LocationERK15AppConversationRK17EChatActionParams",
    getProfileCache: "_ZN18CAppProfileService25GetPersonProfileFromCacheERKN10messagesdk6TargetE"
  };

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
    } catch (_) {
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
        pending = { user_id, cid, uid, ts: Date.now() };
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

  emit({ event: "ready", platform: "darwin", pid: Process.id });
}

// ---------------------------------------------------------------- Windows (x64 MSVC)
function setupWindows() {
  const mods = Process.enumerateModules();
  const mod = mods.find(m => /^(lib)?appbiz\.dll$/i.test(m.name));
  if (!mod) {
    emit({ event: "error", platform: "windows", message: "AppBiz.dll not loaded" });
    return;
  }

  // MSVC x64 std::string (32 bytes): [0..15] buf/ptr, [16..23] size, [24..31] capacity
  function readMsvcStdString(p) {
    try {
      if (!p || p.isNull()) return null;
      const size = p.add(16).readU64().toNumber();
      const cap = p.add(24).readU64().toNumber();
      if (size === 0) return "";
      if (size > 4096 || cap < 15 || cap > 65536 || size > cap) return null;
      const dataPtr = cap >= 16 ? p.readPointer() : p;
      if (dataPtr.isNull()) return null;
      return dataPtr.readUtf8String(size);
    } catch (_) {
      return null;
    }
  }

  function scanMsvcStrings(basePtr, maxBytes) {
    const found = [];
    if (!basePtr || basePtr.isNull()) return found;
    for (let off = 0; off < maxBytes; off += 8) {
      const s = readMsvcStdString(basePtr.add(off));
      if (s !== null && s.length >= 2 && /^[\x20-\x7E\u4e00-\u9fa5]/.test(s)) {
        found.push({ off, val: s });
      }
    }
    return found;
  }

  const exports = mod.enumerateExports();
  const findExp = (cls, fn) => {
    const hit = exports.find(e => e.type === "function" && e.name.includes(cls) && e.name.includes(fn));
    return hit ? hit.address : null;
  };

  const chatOpenAfterAddr = findExp("CChatService", "OpenConversationAfter");
  const getProfileCacheAddr = findExp("CAppProfileService", "GetPersonProfileFromCache");

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
        // MSVC x64: args[0]=this(rcx), args[1]=Location*(rdx), args[2]=AppConversation*(r8)
        const strings = scanMsvcStrings(args[2], 0x400);
        const cidItem = strings.find(x => /^\d+-\d+#\d+@[\w.-]+$/.test(x.val));
        if (!cidItem) return;
        const cid = cidItem.val;
        const m = cid.match(/^(\d+)-(\d+)#\d+@([\w.-]+)$/);
        if (!m) return;
        const idA = m[1], idB = m[2];
        // 对象最靠前出现的纯数字通常为目标买家 uid
        const firstUid = strings.find(x => x.val === idA || x.val === idB);
        const uid = firstUid ? firstUid.val : idA;
        const selfUid = (uid === idA) ? idB : idA;
        const user_id = buildUserId(selfUid, cid);
        if (!user_id) return;

        if (pending) flushPending(null);
        pending = { user_id, cid, uid, ts: Date.now() };
        const cached = profileCache.get(uid);
        if (cached && cached.display_name) {
          flushPending(cached);
        } else {
          flushTimer = setTimeout(() => flushPending(null), 200);
        }
      }
    });
  }

  if (getProfileCacheAddr) {
    Interceptor.attach(getProfileCacheAddr, {
      onEnter(args) {
        this.shouldCapture = false;
        // MSVC x64 非平凡类返回值：args[0]=this(rcx), args[1]=outRetPtr(rdx), args[2]=Target*(r8)
        const targetStrings = scanMsvcStrings(args[2], 0x80);
        const uidItem = targetStrings.find(x => /^\d{6,}$/.test(x.val));
        if (!uidItem) return;
        const uid = uidItem.val;
        const needPending = Boolean(pending && pending.uid === uid);
        if (!needPending && profileCache.has(uid)) return;
        this.shouldCapture = true;
        this.outPtr = args[1];
        this.uid = uid;
      },
      onLeave() {
        if (!this.shouldCapture || !this.outPtr) return;
        try {
          const profilePtr = this.outPtr.readPointer();
          if (profilePtr.isNull()) return;
          const strings = scanMsvcStrings(profilePtr, 0x300);
          let display_name = null;
          let avatar_url = null;
          for (const item of strings) {
            const v = item.val;
            if (!avatar_url && /^https?:\/\//i.test(v)) {
              avatar_url = v;
              continue;
            }
            if (!display_name && !/^\d+$/.test(v) && !/^https?:\/\//i.test(v) &&
                !v.includes("@") && v.length >= 2 && v.length <= 80 && v !== "cnmh") {
              display_name = v;
            }
          }
          const info = { display_name, avatar_url, extra: {} };
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

  emit({
    event: "ready",
    platform: "windows",
    pid: Process.id,
    module: mod.name,
    hooks: {
      chatOpenAfter: Boolean(chatOpenAfterAddr),
      getProfileCache: Boolean(getProfileCacheAddr)
    }
  });
}

if (Process.platform === "darwin") {
  setupDarwin();
} else if (Process.platform === "windows") {
  setupWindows();
} else {
  emit({ event: "error", message: "Unsupported platform: " + Process.platform });
}
