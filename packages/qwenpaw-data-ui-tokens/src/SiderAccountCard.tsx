import type { ReactElement, ReactNode } from 'react';

export type QwenPawDataSiderAccountAction = {
  key: string;
  node: ReactNode;
};

export interface QwenPawDataSiderAccountCardProps {
  hideUsername?: boolean;
  username: string;
  workspaceName: string;
  usernameIcon: ReactNode;
  workspaceIcon: ReactNode;
  usernameAriaLabel?: string;
  workspaceAriaLabel?: string;
  renderUsernameTrigger?: (trigger: ReactElement) => ReactNode;
  renderWorkspaceTrigger?: (trigger: ReactElement) => ReactNode;
  actions: readonly QwenPawDataSiderAccountAction[];
}

/**
 * Shared lower-left identity card for Data Console and Context.
 *
 * Authentication, routing, menus and workspace mutation remain product-owned;
 * this component owns the shared structure, interaction targets and geometry.
 */
export function QwenPawDataSiderAccountCard({
  hideUsername = false,
  username,
  workspaceName,
  usernameIcon,
  workspaceIcon,
  usernameAriaLabel = username,
  workspaceAriaLabel = workspaceName,
  renderUsernameTrigger,
  renderWorkspaceTrigger,
  actions,
}: QwenPawDataSiderAccountCardProps) {
  const usernameTrigger = (
    <button
      className="qwenpaw-data-sider-account-card__identity-row"
      type="button"
      aria-label={usernameAriaLabel}
      title={username}
    >
      <span className="qwenpaw-data-sider-account-card__identity-icon" aria-hidden="true">
        {usernameIcon}
      </span>
      <strong>{username}</strong>
    </button>
  );
  const WorkspaceRow = renderWorkspaceTrigger ? 'button' : 'div';
  const workspaceTrigger = (
    <WorkspaceRow
      className="qwenpaw-data-sider-account-card__identity-row qwenpaw-data-sider-account-card__workspace-row"
      type={renderWorkspaceTrigger ? 'button' : undefined}
      aria-label={workspaceAriaLabel}
      title={workspaceName}
    >
      <span className="qwenpaw-data-sider-account-card__identity-icon" aria-hidden="true">
        {workspaceIcon}
      </span>
      <span>{workspaceName}</span>
    </WorkspaceRow>
  );

  return (
    <section
      className="qwenpaw-data-sider-account-card"
      data-qwenpaw-data-sider-account-card
      aria-label={hideUsername ? workspaceAriaLabel : usernameAriaLabel}
    >
      <div className="qwenpaw-data-sider-account-card__identity">
        {!hideUsername && (renderUsernameTrigger?.(usernameTrigger) ?? usernameTrigger)}
        {renderWorkspaceTrigger?.(workspaceTrigger) ?? workspaceTrigger}
      </div>
      <div
        className="qwenpaw-data-sider-account-card__actions"
        style={{ gridTemplateColumns: `repeat(${actions.length}, minmax(0, 1fr))` }}
      >
        {actions.map((action) => (
          <div className="qwenpaw-data-sider-account-card__action-slot" key={action.key}>
            {action.node}
          </div>
        ))}
      </div>
    </section>
  );
}
