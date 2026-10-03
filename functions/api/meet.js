// Cloudflare Pages Serverless Function: /api/meet
// Dispatches Starfleet Council Briefing directly to Telegram Group via Telegram Bot API

export async function onRequestPost({ request, env }) {
  try {
    const data = await request.json();
    const topic = (data.topic || "").trim();
    const host = data.host || "@AiSoulSettlementBot";
    const watchdogMinutes = data.watchdog_minutes || 15;
    const human = (data.human || "").trim() || (data.caller || "").trim() || "ENTJ指挥官";
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
        `• 🎛️ 【主持席】裁决者 (\`@AiSoulJudgeBot\`)：负责把控研讨主轴、拆解分工、推进议程并最终汇总收敛。`,
        `• ⚖️ 【审计席】结算主机 (\`@AiSoulSettlementBot\`)：负责方案推演、合规与防御型逻辑审计、防漏洞防崩塌。`,
        `• 🐍 【施工席】泥蛇 (\`@AiSoulMudSnakeBot\`)：负责工程定桩、技术可行性验证、代码与落地实现。`
      ];
    } else if (host.includes("MudSnake")) {
      pList = [
        `• 🎛️ 【主持席】泥蛇 (\`@AiSoulMudSnakeBot\`)：负责把控研讨主轴、拆解分工、推进议程并最终汇总收敛。`,
        `• ⚖️ 【审计席】裁决者 (\`@AiSoulJudgeBot\`)：负责架构守门、合规与防御型逻辑审计、防漏洞防崩塌。`,
        `• 🎛️ 【协同席】结算主机 (\`@AiSoulSettlementBot\`)：负责方案推演与会议对账。`
      ];
    } else {
      pList = [
        `• 🎛️ 【主持席】结算主机 (\`@AiSoulSettlementBot\`)：负责把控研讨主轴、拆解分工、推进议程并最终汇总收敛。`,
        `• ⚖️ 【审计席】裁决者 (\`@AiSoulJudgeBot\`)：负责架构守门、合规与防御型逻辑审计、防漏洞防崩塌。`,
        `• 🐍 【施工席】泥蛇 (\`@AiSoulMudSnakeBot\`)：负责工程定桩、技术可行性验证、代码与落地实现。`
      ];
    }

    const mailtoHeader = "[mailto] @AiSoulSettlementBot @AiSoulJudgeBot @AiSoulMudSnakeBot";
    const briefing = `${mailtoHeader}\n\n` +
      `🌾 【人类指挥官 · PocketFleet联席会议启幕指令】\n\n` +
      `各席位请注意，现就以下核心议题召开战队联席研讨会议：\n` +
      `📌 会议议题：${topic}\n` +
      `⏱️ 看门狗监护：${watchdogMinutes} 分钟无进展自动推进，将在${hostDisplayName} ${watchdogMinutes}分钟后没有收到信息、且没有/meetover状态下提醒并推进任务执行。\n\n` +
      `---\n\n` +
      `### 一、 参会席位与战队分工安排\n` +
      pList.join("\n") + `\n\n` +
      `---\n\n` +
      `### 二、 战队出站交互与防回声纪律（全员强制执行）\n` +
      `为确保 Telegram 群内讨论高效推进且彻底杜绝回声死循环，所有节点严格执行以下规范：\n\n` +
      `1. 🎯 【首轮入席就位】：\n` +
      `   - 主持人【${hostDisplayName}】收到本通令后，立即以 \`[Telegram]re:@指定节点;[waitReply]\` 模式正式发表开场分析并向特定节点派发第一轮研讨任务；\n` +
      `   - 其余非主持席位（各参会节点），收到本通令后，请统一使用 \`[Telegram]re:${human};[NoReply]\` 发送入席报到与初步见解，作为参会确认（本网桥将保留人类可见性，但自动消除触发符，防止回声激荡）。\n\n` +
      `2. 🔄 【过程研讨交锋】：\n` +
      `   - 需对方节点作答时，必须标明 \`[waitReply]\`；\n` +
      `   - 纯同步、阶段性成果汇报或免回信时，必须标明 \`[NoReply]\`。\n\n` +
      `3. 🏁 【会议闭幕归档】：\n` +
      `   - 研讨充分并达成共识后，由主持人【${hostDisplayName}】在群内发送 \`/meetover\` 正式结案闭幕，并向${human}完成汇报。\n\n` +
      `---\n\n` +
      `👉 话筒现正式转交给会议主持人【${hostDisplayName}】(\`${host}\`)，请主持人开席，启动第一轮议题剖析与任务分配！`;

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
