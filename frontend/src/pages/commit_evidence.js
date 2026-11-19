import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {esc,date} from '../utils/html.js';

const OUTCOME_LABEL={
  primary:'主库在线直读',
  already_settled:'已追齐副本直读',
  caught_up:'等待后追齐',
  fell_back_stale:'超时退回旧视点',
  stale_now:'立即读旧视点',
  not_caught_up:'超时拒绝',
  no_safe_view:'无安全视点拒绝',
  no_candidate:'无候选拒绝',
};

const eventLabel=e=>{
  if(e.kind==='replicate')return `replicate ${esc(e.table||'')} ${esc(e.batch||'')}`;
  if(e.kind==='commit')return `commit ${esc(e.order||'')}`;
  if(e.kind==='query')return `query ${esc(e.order||'')}`;
  return e.kind;
};

const decisionCell=e=>{
  if(e.decision==='rejected')return '<span class="rejected">拒绝</span>';
  if(e.decision==='duplicate')return '<span class="attribute">同批重试·未重复应用</span>';
  if(e.decision==='committed')return '<span class="attribute">提交</span>';
  if(e.kind==='query')return `${esc(e.node||'—')}<small>视点 ${e.viewpoint}</small>`;
  if(e.kind==='replicate')return `${esc(e.node||'—')}<small>视点 ${e.viewpoint ?? '—'}</small>`;
  return esc(e.node||e.kind);
};

const outcomeText=e=>{
  if(e.kind!=='query')return '';
  const label=OUTCOME_LABEL[e.wait_outcome]||e.wait_outcome||'—';
  const parts=[label];
  if(typeof e.waited_ms==='number')parts.push(`等待 ${e.waited_ms}ms`);
  if(typeof e.viewpoint==='number')parts.push(`视点 ${e.viewpoint}`);
  if(typeof e.freshness_lag==='number')parts.push(`落后 ${e.freshness_lag} 个提交`);
  if(typeof e.data_age_ms==='number')parts.push(`数据龄 ${e.data_age_ms}ms`);
  return parts.join(' · ');
};

const queryDouble=evidence=>{
  const reads=evidence.filter(e=>e.kind==='query'&&e.decision==='routed');
  if(!reads.length)return '';
  const rows=reads.map(e=>[
    `${e.at_ms}ms<small>${esc(e.order_id)}</small>`,
    esc(e.wait_outcome||'—'),
    `${e.waited_ms??0}ms`,
    `${e.viewpoint}`,
    `${e.freshness_lag??0}`,
    `${e.naive_header_version??'—'} / ${e.naive_lines_version??'—'}`,
    e.splice_would_occur?'<span class="rejected">会拼接</span>':'<span class="attribute">同一提交</span>',
  ]);
  return `<div class="section-top"><h2>时效与等待的代价</h2><span>READ AT SAME COMMIT VIEWPOINT</span></div>
${table(['读取时间 / 订单','取得方式','等待','实际视点','落后提交','两表各自位点','拼接风险'],rows)}`;
};

export function commitEvidencePage(trial){
  if(!trial)return '<div class="empty">还没有提交视点证据。先在「提交视点」页编排提交、复制、下线和查询，再运行一次。</div>';
  const r=trial.result;
  const trace=r?r.trace:[];
  const s=r?.summary;
  return `<div class="evidence-top"><div><span class="eyebrow">COMMIT VIEWPOINT EVIDENCE / #${trial.id}</span><h2>${esc(trial.name)} ${badge(trial.status)}</h2><p>${date(trial.created_at)} · 节点与请求快照已冻结 · 主库下线策略：${esc(r?.policy?.primary_down_policy_label||trial.primary_down_policy)}</p></div><button id="commit-export" data-id="${trial.id}">导出 JSON ↓</button></div>
${trial.error?`<div class="error">${esc(trial.error)}。请检查配置后重新执行。</div>`:''}
${r?`<div class="verdict"><div class="verdict-icon">${s.reads_single_viewpoint?'✓':'!'}</div><div><span class="eyebrow">SINGLE COMMIT VIEWPOINT CHECK</span><h2>${s.reads_single_viewpoint?'每次读取都落在同一提交视点':'存在跨提交拼接风险'}</h2><p>查询 ${s.queries} · 成功读取 ${s.routed} · 拒绝 ${s.rejected} · 其中等待 ${s.waited_queries} 次 / 平均 ${s.avg_waited_ms}ms</p></div><div class="equation">${s.splice_avoided}<span> / ${s.reads} 次避免拼接</span></div></div>
<div class="policy-strip">延迟门槛 ${r.policy.max_delay_ms}ms <span>／</span> 等待上限 ${r.policy.wait_ms}ms <span>／</span> 策略 ${esc(r.policy.primary_down_policy_label)} <span>／</span> 谱系 ${esc(r.lineage)} <span>／</span> 已提交位点 ${r.committed_position}</div>
<div class="section-top"><h2>逐事件依据</h2><span>COMMIT / REPLICATION / FAILURE / QUERY</span></div>
${table(['时间 / 事件','会话','节点 / 批次','视点','决策','取得方式与代价'],trace.map(e=>[
  `${e.at_ms}ms<small>${eventLabel(e)}</small>`,
  esc(e.session||'—'),
  `${esc(e.node||'—')}${e.batch?`<small>${esc(e.batch)}</small>`:''}${e.buffered_positions&&e.buffered_positions.length?`<small>暂存 ${e.buffered_positions.join(',')}</small>`:''}`,
  e.viewpoint??(e.kind==='commit'?e.commit_version:'—'),
  decisionCell(e),
  `${esc(outcomeText(e))}${e.reason?`<small>${esc(e.reason)}</small>`:''}${e.alternative?.available?`<small>若立即读旧视点：lag ${e.alternative.freshness_lag}</small>`:''}`,
]))}
${queryDouble(trace)}
<div class="section-top"><h2>终局节点视点</h2><span>VIEWPOINT = MIN(ORDERS, ORDER_LINES)</span></div>
${table(['节点','角色 / 状态','订单头位点','商品行位点','视点','落后提交数'],r.final_nodes.map(n=>[
  `<strong>${esc(n.name)}</strong><small>${esc(n.lineage)}</small>`,
  `${n.role} / ${n.status}`,
  `${n.orders_watermark}`, `${n.lines_watermark}`,
  `<strong>${n.viewpoint}</strong>`, `${n.lag_to_committed}`,
]))}
<div class="evidence-notes"><section><h3>判定口径</h3><p>节点视点取两表连续位的较小值：商品行缺口未补时，即使订单头已到 107 也只读视点 105/106。重复批次按 (表, batch) 键拒绝重复应用。查询视点不足会话位点时拒绝，而不是回表拼接。</p><p>${esc(r.mode)}</p></section><section><h3>确定性摘要</h3><p>相同输入与策略参数产生相同 trace 与摘要；等待窗口来自输入事件投影，不使用随机数或外部网络。</p><code>${esc(r.checksum)}</code></section></div>`
:`<div class="notice">后台重放中，每1.5秒刷新。可保留此页，或稍后从记录列表返回。</div>`}`;
}
