import { Alert, App, Form, Input, Modal } from 'antd';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, errorMessage } from '../api';
import type { ScreenResult, Strategy } from '../types';
import { truncate } from '../utils/format';

interface Values {
  name: string;
  note?: string;
}

export function defaultStrategyName(s: Strategy | null): string {
  return s?.name || truncate(s?.query ?? '', 24) || '未命名策略';
}

/** 保存策略：名称 + 备注 → POST /api/strategies */
export function SaveStrategyModal({
  open,
  strategy,
  result,
  stale,
  onClose,
}: {
  open: boolean;
  strategy: Strategy | null;
  result: ScreenResult | null;
  stale: boolean;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [form] = Form.useForm<Values>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) setError(null);
  }, [open]);

  const submit = async () => {
    if (!strategy) return;
    let values: Values;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const withRun = result && !stale && result.status === 'done';
      const saved = await api.createStrategy({
        name: values.name.trim(),
        note: values.note?.trim() || null,
        strategy: { ...strategy, name: values.name.trim() },
        last_run_id: withRun ? result.run_id : null,
        last_included: withRun ? (result.included ?? []).length : null,
        last_trade_date: withRun ? result.trade_date ?? null : null,
      });
      onClose();
      message.success({
        content: (
          <span>
            已保存「{saved?.name ?? values.name}」
            <a style={{ marginLeft: 8 }} onClick={() => navigate('/library?tab=strategies')}>
              前往我的策略
            </a>
          </span>
        ),
        duration: 5,
      });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title="保存策略"
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      onOk={() => void submit()}
      onCancel={onClose}
      destroyOnHidden
    >
      {stale && (
        <Alert
          style={{ marginBottom: 12 }}
          type="info"
          showIcon
          message="将保存当前编辑中的条件（与结果所用条件不同），不记录本次入选数。"
        />
      )}
      <Form
        form={form}
        layout="vertical"
        preserve={false}
        initialValues={{ name: defaultStrategyName(strategy), note: '' }}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: '请输入策略名称' }]}>
          <Input maxLength={60} placeholder="如：沪深300 经营改善 + 估值合理" />
        </Form.Item>
        <Form.Item name="note" label="备注">
          <Input.TextArea maxLength={300} autoSize={{ minRows: 2, maxRows: 4 }} placeholder="可选：记录口径选择的理由等" />
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon message="保存失败" description={error} />}
    </Modal>
  );
}
