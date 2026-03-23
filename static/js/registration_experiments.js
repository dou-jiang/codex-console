const experimentElements = {
    experimentIdInput: document.getElementById('experiment-id-input'),
    loadExperimentBtn: document.getElementById('load-experiment-btn'),
    experimentSummary: document.getElementById('experiment-summary'),
    experimentStepCompare: document.getElementById('experiment-step-compare'),
    survivalSummary: document.getElementById('survival-summary'),
};

document.addEventListener('DOMContentLoaded', () => {
    if (experimentElements.loadExperimentBtn) {
        experimentElements.loadExperimentBtn.addEventListener('click', () => {
            loadExperimentDashboard();
        });
    }
});

async function loadExperimentDashboard() {
    const experimentId = String(experimentElements.experimentIdInput?.value || '').trim();
    if (!experimentId) {
        toast.warning('请输入实验批次 ID');
        return;
    }

    try {
        const [summary, steps, survival] = await Promise.all([
            api.get(`/registration/experiments/${experimentId}`),
            api.get(`/registration/experiments/${experimentId}/steps`),
            api.get(`/accounts/survival-summary?experiment_batch_id=${encodeURIComponent(experimentId)}`),
        ]);

        renderExperimentSummary(summary);
        renderStepComparison(steps);
        renderSurvivalSummary(survival);
    } catch (error) {
        toast.error(error.message || '加载实验看板失败');
    }
}

function renderExperimentSummary(summary) {
    if (!experimentElements.experimentSummary) return;
    const pipelines = summary?.pipelines || {};
    const cards = Object.entries(pipelines).map(([pipelineKey, value]) => `
        <div class="card experiment-metric-card">
            <div class="card-body">
                <div class="stat-label">${escapeHtml(pipelineKey)}</div>
                <div class="stat-value">${formatRate(value.success_rate)}</div>
                <div class="stat-meta">平均耗时 ${formatDuration(value.avg_duration_ms)}</div>
            </div>
        </div>
    `).join('');

    experimentElements.experimentSummary.innerHTML = `
        <div class="card experiment-overview-card">
            <div class="card-body">
                <div class="stat-label">总任务数</div>
                <div class="stat-value">${summary?.total_tasks ?? 0}</div>
                <div class="stat-meta">状态：${escapeHtml(summary?.status || '-')}</div>
            </div>
        </div>
        ${cards}
    `;
}

function renderStepComparison(payload) {
    if (!experimentElements.experimentStepCompare) return;
    const steps = payload?.steps || [];
    if (steps.length === 0) {
        experimentElements.experimentStepCompare.innerHTML = '<div class="empty-state"><div class="empty-state-title">暂无步骤统计</div></div>';
        return;
    }

    experimentElements.experimentStepCompare.innerHTML = `
        <table class="data-table">
            <thead>
                <tr>
                    <th>Step</th>
                    <th>current_pipeline</th>
                    <th>codexgen_pipeline</th>
                    <th>平均耗时差</th>
                </tr>
            </thead>
            <tbody>
                ${steps.map((step) => `
                    <tr>
                        <td>${escapeHtml(step.step_key || '-')}</td>
                        <td>${formatRate(step.pipelines?.current_pipeline?.success_rate)} / ${formatDuration(step.pipelines?.current_pipeline?.avg_duration_ms)}</td>
                        <td>${formatRate(step.pipelines?.codexgen_pipeline?.success_rate)} / ${formatDuration(step.pipelines?.codexgen_pipeline?.avg_duration_ms)}</td>
                        <td>${formatDuration(step.pipeline_diff?.avg_duration_ms)}</td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

function renderSurvivalSummary(summary) {
    if (!experimentElements.survivalSummary) return;
    const counts = summary?.counts || {};
    experimentElements.survivalSummary.innerHTML = ['healthy', 'warning', 'dead'].map((key) => `
        <div class="card experiment-metric-card">
            <div class="card-body">
                <div class="stat-label">${key}</div>
                <div class="stat-value">${counts[key] ?? 0}</div>
                <div class="stat-meta">${formatRate(summary?.ratios?.[key])}</div>
            </div>
        </div>
    `).join('');
}

function formatRate(value) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
        return '-';
    }
    return `${(value * 100).toFixed(1)}%`;
}

function formatDuration(value) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
        return '-';
    }
    return `${Math.round(value)}ms`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
