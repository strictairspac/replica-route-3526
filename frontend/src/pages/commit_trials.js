import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';

export const POLICY_OPTIONS=[
  ['wait_then_stale','等待副本追齐，超时退回已齐旧视点'],
  ['wait_then_reject','等待副本追齐，超时拒绝读取'],
  ['stale_first','立即读取已齐旧视点，不等待'],
];

export const EVENT_TEMPLATES={
  commit:{at_ms:0,kind:'commit',session:'lab-A',order:'SO-8801'},
  replicate:{at_ms:20,kind:'replicate',node:'south-replica',table:'orders',batch:'po-106',position:106},
  node_down:{at_ms:30,kind:'node_down',node:'harbor-primary'},
  query:{at_ms:40,kind:'query',session:'lab-A',order:'SO-8801'},
};

export const commitDraft={
  rows:[
    {at_ms:'0',kind:'commit',session:'lab-A',order:'SO-8801',node:'',table:'',batch:'',position:''},
    {at_ms:'20',kind:'replicate',session:'',order:'',node:'south-replica',table:'orders',batch:'po-106',position:'106'},
    {at_ms:'40',kind:'query',session:'lab-A',order:'SO-8801',node:'',table:'',batch:'',position:''},
  ],
};

export const rowFields=row=>`<tr data-row>
<td><input name="at_ms" type="number" min="0" value="${esc(row.at_ms)}" aria-label="时间"></td>
<td><select name="kind">${['commit','replicate','node_down','node_up','query'].map(k=>`<option ${row.kind===k?'selected':''}>${k}</option>`).join('')}</select></td>
<td><input name="session" value="${esc(row.session)}" placeholder="session" aria-label="会话"></td>
<td><input name="order" value="${esc(row.order)}" placeholder="SO-…" aria-label="订单"></td>
<td><input name="node" value="${esc(row.node)}" placeholder="节点" aria-label="节点"></td>
<td><select name="table" aria-label="表"><option value="">—</option><option value="orders" ${row.table==='orders'?'selected':''}>orders</option><option value="order_lines" ${row.table==='order_lines'?'selected':''}>order_lines</option></select></td>
<td><input name="batch" value="${esc(row.batch)}" placeholder="batch" aria-label="批次键"></td>
<td><input name="position" type="number" min="0" value="${esc(row.position)}" placeholder="位点" aria-label="位点"></td>
<td><button type="button" class="small remove-row" ${session.user.role==='viewer'?'disabled':''}>删</button></td></tr>`;

export function commitTrialsPage(trials,sets){
  const disabled=session.user.role==='viewer'?'disabled':'';
  return `<div class="split"><section>
<div class="section-top"><h2>提交视点演练记录</h2><span>${trials.length} RUNS / COMMIT VIEWPOINT</span></div>
${table(['演练 / 时间','请求集','主库下线策略','状态','证据'],trials.map(t=>[
  `<strong>${esc(t.name)}</strong><small>${date(t.created_at)}</small>`,
  esc(sets.find(s=>s.id===t.request_set_id)?.code||'—'),
  esc(POLICY_OPTIONS.find(p=>p[0]===t.primary_down_policy)?.[1]||t.primary_down_policy),
  badge(t.status),
  `<a href="#commit_evidence/${t.id}">查看 →</a>`]))}
<div class="protocol"><h3>提交视点规则</h3><ol>
<li>一次 commit 同时写订单头与商品行，并分配同一个提交位点。</li>
<li>复制批次按节点、按表到达，允许乱序；同一 batch 重试只应用一次，缺口回补前视点不推进。</li>
<li>查询只取节点视点 = min(订单头连续位点, 商品行连续位点)，避免把两笔提交拼成一次读取。</li>
<li>会话位点必须满足；主库下线后按策略等待追齐、退回已齐旧视点或拒绝，并记录等待与时效代价。</li>
</ol></div>
<div class="section-top"><h2>编排提交 / 复制 / 下线 / 查询</h2><span>EVENT COMPOSER</span></div>
<div class="table-wrap"><table class="composer"><thead><tr>
<th>时间ms</th><th>事件</th><th>会话</th><th>订单</th><th>节点</th><th>表</th><th>批次键</th><th>位点</th><th></th>
</tr></thead><tbody id="composer-rows">${commitDraft.rows.map(rowFields).join('')}</tbody></table></div>
<p class="composer-actions"><button type="button" id="add-row" ${disabled}>+ 添加事件行</button><button type="button" id="add-commit" ${disabled}>+ 提交</button><button type="button" id="add-replicate" ${disabled}>+ 复制批次</button><button type="button" id="add-down" ${disabled}>+ 主库下线</button><button type="button" id="add-query" ${disabled}>+ 查询</button></p>
</section>
<section class="form-panel"><span class="eyebrow">COMMIT VIEWPOINT REPLAY</span><h2>启动提交视点演练</h2>
<form id="compose-form">
<label>演练名称<input name="name" value="split-replica-1118" required></label>
<div class="fields"><label>延迟门槛ms<input name="max_delay_ms" type="number" min="0" value="100" required></label><label>等待上限ms<input name="wait_ms" type="number" min="0" value="500" required></label></div>
<label>主库下线策略<select name="primary_down_policy">${POLICY_OPTIONS.map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select></label>
<label>请求集编号<input name="code" value="CM-${new Date().toISOString().slice(0,10).replaceAll('-','')}" required></label>
<button class="primary" ${disabled}>编排并运行 ↗</button>
</form>
<p class="help">编排会先保存为新的会话请求集，再冻结节点与请求快照交给后台计算。等待上限只影响主库下线后的查询。</p>
<form id="commit-trial-form" class="reuse-form">
<label>直接复用已有请求集<select name="request_set_id">${sets.filter(s=>s.events.some(e=>e.kind==='commit'||e.kind==='query')).map(s=>`<option value="${s.id}">${esc(s.code)} · ${esc(s.title)}</option>`).join('')||'<option value="">暂无提交视点请求集</option>'}</select></label>
<div class="fields"><label>延迟门槛ms<input name="max_delay_ms" type="number" min="0" value="100"></label><label>等待上限ms<input name="wait_ms" type="number" min="0" value="500"></label></div>
<label>主库下线策略<select name="primary_down_policy">${POLICY_OPTIONS.map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select></label>
<label>演练名称<input name="name" value="reuse-${new Date().toISOString().slice(0,10)}"></label>
<button ${disabled}>复用运行 ↗</button>
</form>
</section></div>`;
}
