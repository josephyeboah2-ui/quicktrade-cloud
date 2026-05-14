import re

with open('server.js', 'r', encoding='utf-8') as f:
    text = f.read()

old_cron = r"""      console.log\("\[QuickTrade\] Market Closed. Running Automated AI Debrief\.\.\."\);
      const scriptPath = path\.join\(__dirname, "\.\./QuickTradeExtension/backend", "daily_debrief\.py"\);
      exec\(`python "\$\{scriptPath\}"`, \(error, stdout, stderr\) => \{
        if \(error\) \{
          console\.error\("\[QuickTrade\] Debrief failed:", error\.message\);
        \} else \{
          console\.log\("\[QuickTrade\] Debrief completed:\\n", stdout\);
        \}
      \}\);"""

new_cron = """      console.log("[QuickTrade] Market Closed. Running Automated AI Debrief...");
      const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend", "daily_debrief.py");
      exec(`python "${scriptPath}"`, (error, stdout, stderr) => {
        if (error) {
          console.error("[QuickTrade] Debrief failed:", error.message);
        } else {
          console.log("[QuickTrade] Debrief completed:\\n", stdout);
          
          console.log("[QuickTrade] Triggering AI Auto-Tuner for parameter evolution...");
          const tunerPath = path.join(__dirname, "../QuickTradeExtension/backend", "auto_tuner.py");
          exec(`python "${tunerPath}"`, (err2, out2, err2_out) => {
            if (err2) {
              console.error("[QuickTrade] Auto-Tuner failed:", err2.message);
            } else {
              console.log("[QuickTrade] Auto-Tuner parameter evolution completed successfully.");
            }
          });
        }
      });"""

text = re.sub(old_cron, new_cron, text)

with open('server.js', 'w', encoding='utf-8') as f:
    f.write(text)
print("done")
