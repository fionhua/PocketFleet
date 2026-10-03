// Cloudflare Pages Serverless Function: /api/meet
// Dispatches Starfleet Council Briefing directly to Telegram Group via Telegram Bot API

export async function onRequestPost({ request, env }) {
  try {
    const data = await request.json();
    const topic = (data.topic || "").trim();
    const host = data.host || "@AiSoulSettlementBot";
    const watchdogMinutes = data.watchdog_minutes || 15;
    const human = data.human || "人类指挥官";
    const chatId = data.chat_id || (env && env.TELEGRAM_CHAT_ID) || "-1004309197838";

    // Starfleet Bot Tokens fallback
    const tokens = {
      "@AiSoulSettlementBot": (env && env.TELEGRAM_BOT_SETTLEMENT_TOKEN) || "8314198693:AAHI13odBzbXvYgq48Zpo154NhuW5d9aQkc",
      "@AiSoulJudgeBot": (env && env.TELEGRAM_BOT_JUDGE_TOKEN) || "8627905935:AAHSN1IOfxDBk3c43yZS2YhD4AkExZozkUo",
      "@AiSoulMudSnakeBot": (env && env.TELEGRAM_BOT_MUDSNAKE_TOKEN) || "8854227609:AAGl5p9JhJY7PzEx7zLMLMGM6-6WL_CoJ-A",
    };

    const botToken = data.bot_token || tokens[host] || tokens["@AiSoulJudgeBot"] || tokens["@AiSoulSettlementBot"];

    if (!botToken) {
      return new Response(JSON.stringify({ ok: false, error: "Bot token not available" }), {
        status: 400,
        headers: { "Content-Type": "application/json" }
      });
    }

    const hostDisplayName = host.includes("Judge") ? "裁决者" : (host.includes("MudSnake") ? "泥蛇" : "结算主机");

    const briefing = `# 🏛️ 【AI星舰战队联席会议公文】\n` +
      `📌 会议议题：${topic}\n` +
      `🌾 召集人：${human}\n` +
      `⏱️ 推进看门狗：每 ${watchdogMinutes} 分钟监护\n\n` +
      `### 一、 参会席位名单与职责 (Participants)\n` +
      `1. 🎛️ 【主持席】结算主机 (\`@AiSoulSettlementBot\`) — 方案推演、会议对账与结论收敛\n` +
      `2. ⚖️ 【审计席】裁决者 (\`@AiSoulJudgeBot\`) — 架构守门、防崩兜底与逻辑审计\n` +
      `3. 🐍 【施工席】泥蛇 (\`@AiSoulMudSnakeBot\`) — 工程落地、核心算法与代码定桩\n\n` +
      `### 二、 会议主持与发信规则（协议级强制遵循）\n` +
      `• 首发主持：由【${hostDisplayName} (${host})】率先开场发言，就议题展开第一手深度剖析与方案推演；\n` +
      `• 出站发信规范（首行带 [Telegram] 投递回群）：\n` +
      `  - 点名交锋（需对方回复）：\`[Telegram];re:@BotId;[waitReply]\` 或 \`[Telegram];mailto:@BotId;[waitReply]\`\n` +
      `  - 结案/纯同步（防死循环）：\`[Telegram];re:@BotId;[NoReply]\`\n` +
      `  - 网桥自动脱敏：检测到 [NoReply] 时，回复仍会发给人类看，但自动脱敏 @ 触发符，彻底阻断回声！\n` +
      `• 会议闭幕：议程达成共识后，由主持人在群内发送 \`/meetover\` 正式结案闭幕。\n\n` +
      `注意：请【${hostDisplayName}】以 \`[Telegram];mailto:@BotId;[waitReply]\` 向【${human}】确认是否需要有补充信息。其他参会节点以 \`[Telegram];mailto:${host};[NoReply]\` 发送“收到，已进入议席”。`;

    const tgUrl = `https://api.telegram.org/bot${botToken}/sendMessage`;
    const resp = await fetch(tgUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: briefing,
        parse_mode: "Markdown"
      })
    });

    const tgRes = await resp.json();
    return new Response(JSON.stringify(tgRes), {
      status: resp.status,
      headers: { "Content-Type": "application/json" }
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { "Content-Type": "application/json" }
    });
  }
}
