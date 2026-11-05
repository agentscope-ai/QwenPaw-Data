import { useOutletContext } from 'react-router-dom';
import type { ConsoleChatController } from '@/features/console/model/useConsoleChat';

export interface ConsoleLayoutContext {
  consoleController: ConsoleChatController;
  openMobileNav: () => void;
}

export function useConsoleLayout(): ConsoleLayoutContext {
  return useOutletContext<ConsoleLayoutContext>();
}
