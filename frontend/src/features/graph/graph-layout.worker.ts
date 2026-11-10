/// <reference lib="webworker" />
import { layoutGraph, type LayoutInput } from './graph-model';
self.onmessage = (event: MessageEvent<LayoutInput>) => self.postMessage(layoutGraph(event.data));
