import {api,download} from './api/client.js';
import {nodesStore} from './stores/nodes.js';
import {request_setsStore} from './stores/request_sets.js';
import {trialsStore} from './stores/trials.js';
import {commit_trialsStore} from './stores/commit_trials.js';
import {session} from './hooks/session.js';
import {feedback} from './hooks/feedback.js';
import {formValues} from './utils/forms.js';
import {esc,date} from './utils/html.js';
import {frame} from './components/common/frame.js';
import {table} from './components/common/table.js';
import {loginPage} from './pages/login.js';
import {nodesPage} from './pages/nodes.js';
import {requestSetsPage} from './pages/request_sets.js';
import {trialsPage} from './pages/trials.js';
import {evidencePage} from './pages/evidence.js';
import {commitTrialsPage,commitDraft,rowFields,EVENT_TEMPLATES} from './pages/commit_trials.js';
import {commitEvidencePage} from './pages/commit_evidence.js';
import {titles} from './router/routes.js';

let timer=null;
const app=document.querySelector('#app');

function bindForm(id,path,transform,after){
  document.querySelector(id)?.addEventListener('submit',async e=>{
    e.preventDefault();
    const b=e.target.querySelector('button');
    b.disabled=true;
    try{
      const data=transform(formValues(e.target));
      const ident=data.id;
      delete data.id;
      const result=await api(path+(ident?'/'+ident:''),ident?'PUT':'POST',data);
      await after(result);
      feedback('保存成功，记录已更新。');
    }catch(err){
      feedback(err.message.includes('JSON')?'事件JSON格式错误，请检查引号与逗号':err.message,true);
    }finally{b.disabled=false;}
  });
}

function readComposer(){
  return [...document.querySelectorAll('#composer-rows tr[data-row]')].map(tr=>
    Object.fromEntries([...tr.querySelectorAll('input,select')].map(el=>[el.name,el.value])));
}

function draftEvents(){
  return readComposer().map(row=>{
    const event={at_ms:Number(row.at_ms),kind:row.kind};
    if(row.session)event.session=row.session;
    if(row.kind==='commit'||row.kind==='query')event.order=row.order;
    if(['replicate','node_down','node_up'].includes(row.kind))event.node=row.node;
    if(row.kind==='replicate'){event.table=row.table;event.batch=row.batch;event.position=Number(row.position);}
    return event;
  });
}

function renderComposerRows(){
  const body=document.querySelector('#composer-rows');
  if(!body)return;
  body.innerHTML=commitDraft.rows.map(row=>rowFields(row)).join('');
  document.querySelectorAll('#composer-rows .remove-row').forEach(b=>b.onclick=()=>{
    commitDraft.rows=readComposer();
    commitDraft.rows.splice([...document.querySelectorAll('#composer-rows tr[data-row]')].indexOf(b.closest('tr')),1);
    renderComposerRows();
  });
}

function nextTime(){
  const rows=readComposer();
  return String(Number(rows.at(-1)?.at_ms||0)+10);
}

function appendEvent(template){
  commitDraft.rows=readComposer();
  commitDraft.rows.push({
    at_ms:nextTime(),kind:template.kind,session:template.session||'',order:template.order||'',
    node:template.node||'',table:template.table||'',batch:template.batch||'',
    position:template.position==null?'':String(template.position),
  });
  renderComposerRows();
}

function bindComposer(){
  if(!document.querySelector('#composer-rows'))return;
  document.querySelector('#add-row')?.addEventListener('click',()=>appendEvent({kind:'query',session:'lab-A',order:'SO-8801'}));
  document.querySelector('#add-commit')?.addEventListener('click',()=>appendEvent(EVENT_TEMPLATES.commit));
  document.querySelector('#add-replicate')?.addEventListener('click',()=>appendEvent(EVENT_TEMPLATES.replicate));
  document.querySelector('#add-down')?.addEventListener('click',()=>appendEvent(EVENT_TEMPLATES.node_down));
  document.querySelector('#add-query')?.addEventListener('click',()=>appendEvent(EVENT_TEMPLATES.query));
}

async function persistComposer(code){
  const events=draftEvents();
  if(!events.length)throw Error('至少需要一条编排事件');
  const unique=code+'-'+Date.now().toString().slice(-5);
  const result=await api('/request_sets','POST',{code:unique,title:'提交视点编排 '+code,events,note:'订单头与商品行分行复制'});
  commitDraft.rows=readComposer();
  return result;
}

async function render(){
  clearTimeout(timer);
  if(!session.token){
    app.innerHTML=loginPage();
    bindForm('#login','/auth/login',x=>x,async x=>{session.save(x);location.hash='nodes';await render();});
    return;
  }
  try{
    await api('/auth/me');
    await Promise.all([nodesStore.refresh(),request_setsStore.refresh(),trialsStore.refresh(),commit_trialsStore.refresh()]);
    const [page='nodes',id]=location.hash.slice(1).split('/');
    const nodes=nodesStore.items,sets=request_setsStore.items,trials=trialsStore.items,commitTrials=commit_trialsStore.items;
    let content,trial;
    if(page==='request_sets')content=requestSetsPage(sets);
    else if(page==='trials')content=trialsPage(trials,sets);
    else if(page==='evidence'){trial=id?await api('/trials/'+id):trials[0];content=evidencePage(trial);}
    else if(page==='commit_trials')content=commitTrialsPage(commitTrials,sets);
    else if(page==='commit_evidence'){trial=id?await api('/commit_trials/'+id):commitTrials[0];content=commitEvidencePage(trial);}
    else if(page==='audit'){const rows=await api('/audit');content=table(['时间','操作人','动作'],rows.map(x=>[date(x.created_at),esc(x.actor),esc(x.action)]));}
    else content=nodesPage(nodes);

    const [title,subtitle]=titles[page]||titles.nodes;
    app.innerHTML=frame(page,title,subtitle,content);
    document.querySelector('#logout').onclick=()=>{session.clear();render();};
    bindForm('#node-form','/nodes',x=>({...x,position:Number(x.position),delay_ms:Number(x.delay_ms)}),render);
    bindForm('#request-form','/request_sets',x=>({...x,events:JSON.parse(x.events)}),render);
    bindForm('#trial-form','/trials',x=>({...x,request_set_id:Number(x.request_set_id),max_delay_ms:Number(x.max_delay_ms),sticky_ms:Number(x.sticky_ms)}),async x=>{location.hash='evidence/'+x.id;await render();});
    document.querySelector('#commit-trial-form')?.addEventListener('submit',async e=>{
      e.preventDefault();
      const b=e.target.querySelector('button');b.disabled=true;
      try{
        const data=formValues(e.target);
        if(!data.request_set_id)throw Error('还没有可复用的提交视点请求集，请先编排一次');
        const result=await api('/commit_trials','POST',{name:data.name,request_set_id:Number(data.request_set_id),max_delay_ms:Number(data.max_delay_ms),wait_ms:Number(data.wait_ms),primary_down_policy:data.primary_down_policy});
        location.hash='commit_evidence/'+result.id;await render();
      }catch(err){feedback(err.message,true);}finally{b.disabled=false;}
    });
    document.querySelector('#compose-form')?.addEventListener('submit',async e=>{
      e.preventDefault();
      const b=e.target.querySelector('button');b.disabled=true;
      try{
        const data=formValues(e.target);
        const saved=await persistComposer(data.code);
        const result=await api('/commit_trials','POST',{name:data.name,request_set_id:saved.id,max_delay_ms:Number(data.max_delay_ms),wait_ms:Number(data.wait_ms),primary_down_policy:data.primary_down_policy});
        location.hash='commit_evidence/'+result.id;await render();
      }catch(err){feedback(err.message,true);}finally{b.disabled=false;}
    });
    bindComposer();

    document.querySelectorAll('.edit-node').forEach(b=>b.onclick=()=>{const n=nodes.find(x=>x.id===Number(b.dataset.id)),form=document.querySelector('#node-form');for(const key of ['id','name','role','status','lineage','position','delay_ms','note'])form.elements[key].value=n[key];document.querySelector('#node-heading').textContent='修改节点 #'+n.id;form.scrollIntoView({behavior:'smooth',block:'center'});});
    document.querySelectorAll('.edit-request').forEach(b=>b.onclick=()=>{const s=sets.find(x=>x.id===Number(b.dataset.id)),form=document.querySelector('#request-form');for(const key of ['id','code','title','note'])form.elements[key].value=s[key];form.elements.events.value=JSON.stringify(s.events,null,2);document.querySelector('#request-heading').textContent='修改请求集 #'+s.id;form.scrollIntoView({behavior:'smooth',block:'center'});});
    document.querySelector('#export')?.addEventListener('click',async e=>{try{await download(e.target.dataset.id);feedback('路由JSON已下载');}catch(err){feedback(err.message,true);}});
    document.querySelector('#commit-export')?.addEventListener('click',async e=>{try{await download(e.target.dataset.id,'commit');feedback('提交视点JSON已下载');}catch(err){feedback(err.message,true);}});

    if((page==='evidence'&&trial&&['queued','running'].includes(trial.status))
      ||(page==='commit_evidence'&&trial&&['queued','running'].includes(trial.status))
      ||(page==='trials'&&trials.some(x=>['queued','running'].includes(x.status)))
      ||(page==='commit_trials'&&commitTrials.some(x=>['queued','running'].includes(x.status))))timer=setTimeout(render,1500);
  }catch(err){
    if(err.message.includes('登录')||err.message.includes('Not authenticated')){session.clear();await render();}
    else{if(!document.querySelector('#feedback'))app.innerHTML='<main><div id="feedback"></div><button onclick="location.reload()">重新加载</button></main>';feedback(err.message,true);}
  }
}
window.addEventListener('hashchange',render);
render();
