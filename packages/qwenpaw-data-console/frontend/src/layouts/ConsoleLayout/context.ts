import { useOutletContext } from 'react-router-dom';
import type { ConsoleChatController } from '@/features/console/model/chat/useConsoleChat';
import type { SettlementCenterController } from '@/features/console/model/settlement/useSettlementCenter';

export interface ConsoleLayoutContext {
  consoleController: ConsoleChatController;
  settlementController: SettlementCenterController;
  openMobileNav: () => void;
}

export function useConsoleLayout(): ConsoleLayoutContext {
  return useOutletContext<ConsoleLayoutContext>();
}
