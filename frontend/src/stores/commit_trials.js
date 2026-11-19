import {commit_trialsApi} from '../api/commit_trials.js';
export const commit_trialsStore={items:[],async refresh(){this.items=await commit_trialsApi.list();return this.items;}};
