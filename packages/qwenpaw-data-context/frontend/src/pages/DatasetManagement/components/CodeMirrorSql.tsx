import React, { useState } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { sql } from '@codemirror/lang-sql';
import { githubLight } from '@uiw/codemirror-theme-github';
import { useTranslation } from 'react-i18next';

interface CodeMirrorSqlProps {
    /** 占位提示文字 */
    placeholder?: string;
    /** 是否可编辑，false 时为只读模式 */
    editable?: boolean;
    /** 受控模式下的值 */
    value?: string;
    /** 值变化时的回调 */
    onChange?: (value: string) => void;
    /** 编辑器高度，默认 200px */
    height?: string;
    /** 自定义 className */
    className?: string;
}

/**
 * SQL 代码编辑器组件
 * - 支持受控模式（传入 value 和 onChange）
 * - 支持非受控模式（不传 value，使用内部 state）
 * - 与 Ant Design Form.Item 兼容
 */
const CodeMirrorSql: React.FC<CodeMirrorSqlProps> = ({
    placeholder,
    editable = true,
    value,
    onChange,
    height = '200px',
    className,
}) => {
    const { t } = useTranslation();
    // 非受控模式使用内部 state
    const [internalValue, setInternalValue] = useState('');

    // 判断是否为受控模式：当外部传入了 value prop 时为受控模式
    const isControlled = value !== undefined;
    const currentValue = isControlled ? value : internalValue;

    const handleChange = (val: string) => {
        // 非受控模式更新内部状态
        if (!isControlled) {
            setInternalValue(val);
        }
        // 无论受控还是非受控，都触发 onChange 回调
        onChange?.(val);
    };

    // 只读模式的样式
    const readOnlyStyle: React.CSSProperties = React.useMemo(() => !editable
        ? { fontSize: '14px', backgroundColor: '#f5f5f5' }
        : { fontSize: '14px' }, [editable]);

    return (
        <CodeMirror
            value={currentValue}
            height={height}
            theme={githubLight}
            extensions={[sql()]}
            onChange={handleChange}
            editable={editable}
            placeholder={placeholder ?? t('validation.inputSql')}
            className={className}
            style={readOnlyStyle}
        />
    );
};

export default CodeMirrorSql;
