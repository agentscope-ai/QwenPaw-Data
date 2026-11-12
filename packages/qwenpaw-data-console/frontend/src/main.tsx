import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './index.css';
import { bootstrapHostAuth } from './services/auth';

async function bootstrap() {
  try {
    if (!(await bootstrapHostAuth())) return;
  } catch (error) {
    console.error('Unable to initialize Host authentication.', error);
  }

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void bootstrap();
