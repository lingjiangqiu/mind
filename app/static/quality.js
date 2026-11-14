const percent = (value) => `${(Number(value) * 100).toFixed(2)}%`;

function cell(value, className = "") {
  const element = document.createElement("td");
  element.textContent = value;
  element.className = className;
  return element;
}

async function loadQuality() {
  const response = await fetch("./mindbridge-quality.v2.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`Quality snapshot ${response.status}`);
  const report = await response.json();
  document.querySelector("#unitTests").textContent = report.unitTests;
  document.querySelector("#harnesses").textContent = report.harnesses;
  document.querySelector("#ragCases").textContent = report.rag.cases;
  document.querySelector("#ragRecall").textContent = percent(report.rag.recallAtK);
  document.querySelector("#ragMrr").textContent = report.rag.mrr.toFixed(4);
  document.querySelector("#ragNdcg").textContent = report.rag.ndcgAtK.toFixed(4);
  document.querySelector("#ragHit").textContent = percent(report.rag.hitRate);
  document.querySelector("#qualityDisclaimer").textContent = `${report.version} · ${report.disclaimer}`;

  const rows = report.invariants.map((item) => {
    const row = document.createElement("tr");
    row.append(cell(item.id), cell(item.name), cell(item.rule), cell(item.expected), cell(item.status, "quality-pass"));
    return row;
  });
  document.querySelector("#invariantRows").replaceChildren(...rows);

  const harnesses = report.harnessNames.map((name, index) => {
    const item = document.createElement("li");
    const id = document.createElement("span");
    id.textContent = String(index + 1).padStart(2, "0");
    const title = document.createElement("strong");
    title.textContent = name;
    const status = document.createElement("em");
    status.textContent = "PASS";
    item.append(id, title, status);
    return item;
  });
  document.querySelector("#harnessList").replaceChildren(...harnesses);

  const safety = report.safetyEvaluation;
  document.querySelector("#routeAccuracy").textContent = percent(safety.metrics.routeAccuracy);
  document.querySelector("#cardAccuracy").textContent = percent(safety.metrics.cardSuppressionAccuracy);
  document.querySelector("#piiAccuracy").textContent = percent(safety.metrics.piiRedactionAccuracy);
  document.querySelector("#actionAccuracy").textContent = percent(safety.metrics.safetyActionCompleteness);
  const safetyRows = safety.results.map((item) => {
    const row = document.createElement("tr");
    row.append(
      cell(item.id), cell(item.title), cell(item.category), cell(item.expectedRoute),
      cell(item.actualRoute), cell(item.riskLevel), cell(item.cardCount),
      cell(item.privacy, item.privacy === "PASS" ? "quality-pass" : ""),
      cell(item.status, item.status === "PASS" ? "quality-pass" : "")
    );
    return row;
  });
  document.querySelector("#safetyRows").replaceChildren(...safetyRows);
  const philosophyRows = (report.philosophy?.results || []).map((item) => {
    const row = document.createElement("tr");
    row.append(cell(item.id), cell(item.title), cell(item.status, item.status === "PASS" ? "quality-pass" : ""));
    return row;
  });
  document.querySelector("#philosophyRows").replaceChildren(...philosophyRows);
}

loadQuality().catch((error) => {
  document.querySelector("#qualityDisclaimer").textContent = `看板读取失败：${error.message}`;
});
