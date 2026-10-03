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

    if (data.action === "close" || topic === "/meetover") {
      const closingCard = `🏁 *【AI星舰战队联席会议 · 结案闭幕】*\n` +
        `🌾 召集人：${human}\n` +
        `📌 结案状态：研讨完成，议程顺利收敛！\n\n` +
        `⏱️ 会议推进看门狗守护已正式撤除。感谢各参会席位的深度推演与协同定桩！`;
      const resp = await fetch(tgUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: chatId,
          text: closingCard,
          parse_mode: "Markdown"
        })
      });
      const tgRes = await resp.json();
      return new Response(JSON.stringify(tgRes), {
        status: resp.status,
        headers: { "Content-Type": "application/json" }
      });
    }

    const hostDisplayName = host.includes("Judge") ? "裁决者" : (host.includes("MudSnake") ? "泥蛇" : "结算主机");

    let pList = [];
    if (host.includes("Judge")) {
      pList = [
        `• ⚖️ 裁决者 (\`@AiSoulJudgeBot\`) — 架构守门与审计 【主持人】`,
        `• 🎛️ 结算主机 (\`@AiSoulSettlementBot\`) — 方案推演与对账`,
        `• 🐍 泥蛇 (\`@AiSoulMudSnakeBot\`) — 工程定桩与算法落地`
      ];
    } else if (host.includes("MudSnake")) {
      pList = [
        `• 🐍 泥蛇 (\`@AiSoulMudSnakeBot\`) — 工程定桩与算法落地 【主持人】`,
        `• 🎛️ 结算主机 (\`@AiSoulSettlementBot\`) — 方案推演与对账`,
        `• ⚖️ 裁决者 (\`@AiSoulJudgeBot\`) — 架构守门与审计`
      ];
    } else {
      pList = [
        `• 🎛️ 结算主机 (\`@AiSoulSettlementBot\`) — 方案推演与对账 【主持人】`,
        `• ⚖️ 裁决者 (\`@AiSoulJudgeBot\`) — 架构守门与审计`,
        `• 🐍 泥蛇 (\`@AiSoulMudSnakeBot\`) — 工程定桩与算法落地`
      ];
    }

    const briefing = `🏛️ *【AI 星舰联席会议已召集】*\n\n` +
      `📌 *议题*：${topic}\n` +
      `🌾 *召集人*：${human}\n` +
      `🎛️ *主持人*：${hostDisplayName} (\`${host}\`)\n` +
      `👥 *参会席位*：\n` +
      pList.join("\n") + `\n` +
      `⏱️ *看门狗*：${watchdogMinutes} 分钟无有效会议进展自动提醒\n\n` +
      `_${hostDisplayName} 正在组织第一轮研讨与分工……_`;

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
