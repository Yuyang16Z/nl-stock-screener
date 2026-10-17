import { Alert, App, Form, Input, Modal, Radio } from 'antd';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, errorMessage } from '../api';
import type { MonitorFrequency, Strategy } from '../types';
import { defaultStrategyName } from './SaveStrategyModal';

interface Values {
  name: string;
  frequency: MonitorFrequency;
}

/** 转为监控：名称 + 频率 → POST /api/monitors */
export function MonitorModal({
  open,
  strategy,
  stale,
  onClose,
}: {
  open: boolean;
  strategy: Strategy | null;
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
      const m = await api.createMonitor({
        name: values.name.trim(),
        strategy: { ...strategy, name: strategy.name ?? values.name.trim() },
        frequency: values.frequency,
        enabled: true,
      });
      onClose();
      message.success({
        content: (
          <span>
            已创建监控「{m?.name ?? values.name}」
            <a style={{ marginLeft: 8 }} onClick={() => navigate('/library?tab=monitors')}>
              查看监控任务
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
      title="转为监控任务"
      okText="创建监控"
      cancelText="取消"
      confirmLoading={saving}
      onOk={() => void submit()}
      onCancel={onClose}
      destroyOnHidden
    >
      <Alert
        style={{ marginBottom: 12 }}
        type="info"
        showIcon
        message="监控会按频率重新执行这组条件，并与上一次结果对比新增 / 移除（附原因）。"
        description="数据集部分失败或失败时会生成数据告警，不会报告“无变化”。监控只描述条件匹配情况，不构成任何买卖依据。"
      />
      {stale && (
        <Alert style={{ marginBottom: 12 }} type="warning" showIcon message="将使用当前编辑中的条件（与结果所用条件不同）。" />
      )}
      <Form
        form={form}
        layout="vertical"
        preserve={false}
        initialValues={{ name: `监控：${defaultStrategyName(strategy)}`, frequency: 'daily' }}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: '请输入监控名称' }]}>
          <Input maxLength={60} />
        </Form.Item>
        <Form.Item name="frequency" label="运行频率">
          <Radio.Group
            optionType="button"
            options={[
              { label: '每日（交易日）', value: 'daily' },
              { label: '每周', value: 'weekly' },
            ]}
          />
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon message="创建失败" description={error} />}
    </Modal>
  );
}
