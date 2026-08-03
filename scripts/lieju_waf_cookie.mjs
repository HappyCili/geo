import fs from "node:fs";
import vm from "node:vm";

const page = fs.readFileSync(0, "utf8");
const renderData = page.match(
  /<[^>]+id=["']renderData["'][^>]*>([\s\S]*?)<\/[^>]+>/i,
);
const scripts = [...page.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)].map(
  (match) => match[1],
);

if (!renderData || scripts.length === 0) {
  process.exitCode = 1;
} else {
  let cookie = "";
  const location = {
    href: "https://www.lieju.com/",
    reload() {},
    assign() {},
    replace() {},
  };
  const document = {
    getElementById(id) {
      return id === "renderData" ? { innerHTML: renderData[1] } : null;
    },
    get referrer() {
      return "";
    },
    location,
    set cookie(value) {
      cookie = String(value);
    },
    get cookie() {
      return cookie;
    },
  };
  const context = {
    document,
    location,
    navigator: {
      language: "zh-CN",
      platform: "MacIntel",
      userAgent: "Mozilla/5.0",
    },
    setTimeout(callback) {
      if (typeof callback === "function") callback();
      return 0;
    },
    clearTimeout() {},
  };
  context.window = context;
  context.self = context;
  context.top = context;
  context.parent = context;
  document.defaultView = context;

  try {
    for (let index = 0; index < scripts.length; index += 1) {
      vm.runInNewContext(scripts[index], context, {
        filename: `lieju-waf-${index}.js`,
        timeout: 1500,
      });
    }
    if (!cookie.startsWith("acw_sc__v2=")) process.exitCode = 1;
  } catch {
    process.exitCode = 1;
  }

  if (process.exitCode !== 1) {
    process.stdout.write(JSON.stringify({ cookie }));
  }
}
