export type PasswordViolation = 'minLength' | 'categoryCount' | 'commonPassword';

const COMMON_PASSWORDS = new Set([
  'password',
  '123456',
  '12345678',
  'qwerty',
  'abc123',
  'monkey',
  '1234567',
  'letmein',
  'trustno1',
  'dragon',
  'baseball',
  'iloveyou',
  'master',
  'sunshine',
  'ashley',
  'bailey',
  'passw0rd',
  'shadow',
  '123123',
  '654321',
  'superman',
  'qazwsx',
  'michael',
  'football',
  'password1',
  'password123',
  'batman',
  'login',
  'welcome',
  'solo',
  'hello',
  'charlie',
  'donald',
  'loveme',
  'princess',
  'starwars',
  'admin',
  'admin123',
  'root',
  'toor',
  'pass',
  'test',
  'guest',
  'changeme',
  'default',
  '12345',
  '123456789',
  '1234567890',
  'password2',
  'qwerty123',
  '1q2w3e4r',
  'aa123456',
  '000000',
  '111111',
  '121212',
  '888888',
  '666666',
  '987654321',
  '1qaz2wsx',
  'qwe123',
  '1q2w3e',
  'zxcvbnm',
  'asdfghjkl',
  'abcd1234',
  'p@ssw0rd',
  'p@ssword',
  'passwd',
  'pass123',
  'pass1234',
  'access',
  'mustang',
  'letmein1',
  'joshua',
  'michael1',
  'jordan',
  'hunter',
  'buster',
  'soccer',
  'harley',
  'ranger',
  'thomas',
  'george',
  'robert',
  'daniel',
  'andrea',
  'andrew',
  'nicole',
  'jessica',
  'jennifer',
  'amanda',
  'melissa',
  'michelle',
  'william',
  'computer',
  'internet',
  'qwert',
  'asdfgh',
  'zxcvbn',
  'corvette',
  '1234',
  '12341234',
  'iloveu',
  'trustno',
  'freedom',
  'whatever',
  'ginger',
  'pepper',
  'summer',
  'winter',
  'spring',
  'autumn',
  'diamond',
  'matrix',
  'killer',
  'phoenix',
  'thunder',
  'austin',
  'jasper',
  'cookie',
]);

const ASCII_PUNCTUATION = `!"#$%&'()*+,-./:;<=>?@[\\]^_\`{|}~`;

export function countPasswordCategories(password: string): number {
  return [
    /[A-Z]/.test(password),
    /[a-z]/.test(password),
    /\d/.test(password),
    [...password].some((character) => ASCII_PUNCTUATION.includes(character)),
  ].filter(Boolean).length;
}

export function getPasswordViolations(password: string): PasswordViolation[] {
  const violations: PasswordViolation[] = [];
  if (password.length < 8) violations.push('minLength');
  if (countPasswordCategories(password) < 3) violations.push('categoryCount');
  if (COMMON_PASSWORDS.has(password.toLowerCase())) {
    violations.push('commonPassword');
  }
  return violations;
}

export function isPasswordValid(password: string): boolean {
  return getPasswordViolations(password).length === 0;
}

export function createPasswordValidator(
  translate: (key: string) => string,
): (_rule: unknown, value?: string) => Promise<void> {
  return async (_rule, value) => {
    if (!value) return;
    const violation = getPasswordViolations(value)[0];
    if (violation) {
      throw new Error(translate(`validation.password.${violation}`));
    }
  };
}
