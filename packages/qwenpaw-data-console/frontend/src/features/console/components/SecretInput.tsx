import { Input } from '@agentscope-ai/design';
import type { ComponentProps } from 'react';

type PasswordInputProps = ComponentProps<typeof Input.Password>;

export default function SecretInput(props: PasswordInputProps) {
  return <Input.Password {...props} visibilityToggle={Boolean(props.value)} />;
}
