import {api} from './client.js';
export const commit_trialsApi={
  list:()=>api('/commit_trials'),
  create:data=>api('/commit_trials','POST',data),
  detail:id=>api('/commit_trials/'+id),
};
