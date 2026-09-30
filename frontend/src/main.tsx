import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { bootstrapAuth } from './auth';
import '@fontsource-variable/dm-sans';
import '@fontsource/instrument-serif/400.css';
import './style.css';

const authentication = bootstrapAuth();
createRoot(document.getElementById('root')!).render(
  <StrictMode><App authentication={authentication} /></StrictMode>,
);
