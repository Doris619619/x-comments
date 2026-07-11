/**
 * 本文件负责 POC 页面任务创建、有限轮询和数据库商品分页展示。
 *
 * 它只调用本项目 REST API，不直接访问闲鱼，也不保存登录态或硬编码商品。
 */

const POLL_INTERVAL_MS = 1500;
const MAX_POLL_ATTEMPTS = 120;
const PAGE_SIZE = 12;

const form = document.querySelector("#crawl-form");
const keywordInput = document.querySelector("#keyword");
const submitButton = document.querySelector("#submit-button");
const message = document.querySelector("#message");
const stats = document.querySelector("#job-stats");
const itemsContainer = document.querySelector("#items");
const template = document.querySelector("#item-template");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");
const pageLabel = document.querySelector("#page-label");
const resultSummary = document.querySelector("#result-summary");

let currentPage = 1;
let totalPages = 0;

/**
 * 请求 JSON API，并把非成功响应转换为可读异常。
 *
 * @param {string} url API URL。
 * @param {RequestInit} options fetch 选项。
 * @returns {Promise<object>} 解析后的 JSON。
 * @throws {Error} 网络或 HTTP 失败时抛出；副作用为一次本项目 API 请求。
 */
async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `请求失败（HTTP ${response.status}）`);
  }
  return body;
}

/**
 * 更新任务状态与统计区域。
 *
 * @param {object} job 后端任务对象。
 * @returns {void} 无返回；副作用为更新 DOM。
 */
function renderJob(job) {
  stats.hidden = false;
  document.querySelector("#job-status").textContent = job.status;
  document.querySelector("#discovered-count").textContent = job.discovered_count;
  document.querySelector("#new-count").textContent = job.new_count;
  document.querySelector("#updated-count").textContent = job.updated_count;
  document.querySelector("#duplicate-count").textContent = job.duplicate_count;
  document.querySelector("#error-count").textContent = job.error_count;
}

/**
 * 生成单张数据库商品卡片。
 *
 * @param {object} item 商品响应对象。
 * @returns {DocumentFragment} 可插入页面的卡片；无网络副作用。
 */
function buildItemCard(item) {
  const fragment = template.content.cloneNode(true);
  const image = fragment.querySelector("img");
  image.src = item.image_url || "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='300'%3E%3Crect width='100%25' height='100%25' fill='%23ecebe6'/%3E%3C/svg%3E";
  image.alt = item.title;
  fragment.querySelector("h3").textContent = item.title;
  fragment.querySelector(".price").textContent = `¥${Number(item.price).toFixed(2)}`;
  fragment.querySelector(".location").textContent = item.location || "地区未提供";
  const link = fragment.querySelector("a");
  link.href = item.item_url;
  return fragment;
}

/**
 * 从数据库 API 加载当前关键词的商品分页。
 *
 * @param {number} page 目标页码。
 * @returns {Promise<void>} 无返回；失败时抛出；副作用为 API 请求和 DOM 更新。
 */
async function loadItems(page = 1) {
  const keyword = keywordInput.value.trim();
  const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
  if (keyword) query.set("keyword", keyword);
  const data = await requestJson(`/api/v1/items?${query}`);
  currentPage = data.page;
  totalPages = data.pages;
  itemsContainer.replaceChildren(...data.items.map(buildItemCard));
  if (data.items.length === 0) {
    itemsContainer.textContent = "数据库中暂时没有该关键词的商品。";
  }
  resultSummary.textContent = `共 ${data.total} 条数据库商品`;
  pageLabel.textContent = `第 ${currentPage} 页 / 共 ${Math.max(totalPages, 1)} 页`;
  previousPage.disabled = currentPage <= 1;
  nextPage.disabled = totalPages === 0 || currentPage >= totalPages;
}

/**
 * 在有限次数内轮询任务，保证页面不会无限等待。
 *
 * @param {string} jobId 任务 ID。
 * @returns {Promise<object>} 最终任务；超时或失败终态抛出 Error。
 */
async function pollJob(jobId) {
  for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt += 1) {
    const job = await requestJson(`/api/v1/crawl-jobs/${jobId}`);
    renderJob(job);
    if (["succeeded", "partially_succeeded"].includes(job.status)) return job;
    if (["failed", "blocked_by_auth_or_risk_control"].includes(job.status)) {
      throw new Error(job.error_message || `任务结束：${job.status}`);
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
  throw new Error("任务轮询超时，请稍后使用任务 ID 查询状态。");
}

/**
 * 创建任务、轮询终态并刷新数据库商品。
 *
 * @param {SubmitEvent} event 表单提交事件。
 * @returns {Promise<void>} 无返回；失败会展示明确错误；副作用为任务创建和页面更新。
 */
async function submitCrawl(event) {
  event.preventDefault();
  submitButton.disabled = true;
  message.className = "message";
  try {
    const job = await requestJson("/api/v1/crawl-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword: keywordInput.value.trim() }),
    });
    renderJob(job);
    message.textContent = `任务 ${job.job_id} 已创建，正在等待执行。`;
    const completed = await pollJob(job.job_id);
    message.textContent = `任务完成：发现 ${completed.discovered_count} 条商品。`;
    message.className = "message success";
    await loadItems(1);
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "任务执行失败";
    message.className = "message error";
  } finally {
    submitButton.disabled = false;
  }
}

form.addEventListener("submit", submitCrawl);
previousPage.addEventListener("click", () => loadItems(currentPage - 1).catch(showLoadError));
nextPage.addEventListener("click", () => loadItems(currentPage + 1).catch(showLoadError));

/**
 * 展示商品加载错误。
 *
 * @param {unknown} error 捕获的异常。
 * @returns {void} 无返回；副作用为更新 DOM。
 */
function showLoadError(error) {
  resultSummary.textContent = error instanceof Error ? error.message : "商品加载失败";
}

loadItems().catch(showLoadError);

