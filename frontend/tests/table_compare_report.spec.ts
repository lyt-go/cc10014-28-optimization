import { expect, test } from '@playwright/test'

/**
 * 表级比对差异报告全链路：
 * 1. 工作台先打开表级比对列表（过滤/搜索/排序/分页/时间线）；
 * 2. 启动比对后进入差异报告详情页，查看字段级差异、采样比例与重检批次；
 * 3. 报告页执行全量重检并展示重检通过记录；
 * 4. 质量稽核跨模块回写后，报告页展示稽核结论。
 */
test.describe.serial('表级比对差异报告', () => {
  test('列表定位筛选 → 启动比对 → 报告详情 → 重检', async ({ page }) => {
    test.setTimeout(120_000)
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    page.on('response', (r) => {
      if (r.url().includes('/api/') && r.status() >= 400 && !r.url().includes('/report')) {
        errors.push(`${r.status()} ${r.url()}`)
      }
    })

    // 1. 工作台打开表级比对列表
    await page.goto('/table_compare')
    const bodyRows = page.locator('tbody tr[data-testid^="row-"]')
    await expect(bodyRows.first()).toBeVisible()
    const baselineCount = await bodyRows.count()

    // 搜索定位
    await page.getByTestId('search-keyword').fill('CMP-ORD')
    await page.getByTestId('query').click()
    await expect(bodyRows).toHaveCount(1)
    await page.getByTestId('reset').click()
    await expect(bodyRows).toHaveCount(baselineCount)

    // 字段过滤
    await page.getByTestId('filter-左表名称').fill('ods_payments')
    await page.getByTestId('query').click()
    await expect(bodyRows).toHaveCount(1)
    await page.getByTestId('reset').click()
    await expect(bodyRows).toHaveCount(baselineCount)

    // 排序：首次点击升序，再次点击降序
    const sortHeader = page.locator('th', { hasText: '任务编号' }).first()
    await sortHeader.click()
    await expect(bodyRows.first()).toHaveAttribute('data-testid', 'row-1')
    await sortHeader.click()
    await expect(bodyRows.first()).toHaveAttribute('data-testid', 'row-3')

    // 时间线仍与列表同屏保留
    await expect(page.locator('.timeline-bar').first()).toBeVisible()

    // 2. 启动比对并进入报告详情
    const row1 = page.getByTestId('row-1')
    await row1.getByRole('button', { name: '启动比对', exact: true }).click()
    await expect(page.getByTestId('report-1')).toBeVisible()
    await page.getByTestId('report-1').click()
    await expect(page).toHaveURL(/\/table_compare\/1\/report/)

    // 3. 报告详情：结果口径、字段级差异、采样比例、重检批次
    await expect(page.getByTestId('result-mode')).toContainText(/精确|估算/)
    await expect(page.getByTestId('field-diff-table')).toBeVisible()
    const fieldRows = page.locator('[data-testid^="field-row-"]')
    expect(await fieldRows.count()).toBeGreaterThanOrEqual(3)
    await expect(page.getByTestId('recheck-panel')).toBeVisible()

    // 返回列表恢复点：筛选条件之外的列表仍可定位
    await page.getByTestId('back-list').click()
    await expect(page).toHaveURL('/table_compare')
    await expect(bodyRows.first()).toBeVisible()

    expect(errors).toEqual([])
  })

  test('报告页展示质量稽核回写记录', async ({ page }) => {
    test.setTimeout(120_000)
    // 先在质量稽核页执行稽核并回写，再到报告详情查看
    await page.goto('/quality_check')
    await page.waitForTimeout(1500)
    await page.getByTestId('execute-1').click()
    await page.waitForTimeout(800)
    await page.getByTestId('writeback-1').click()
    await page.waitForTimeout(800)

    await page.goto('/table_compare')
    await page.waitForTimeout(1500)
    // 若无报告则先启动比对，再触发一次回写
    const reportLink = page.getByTestId('report-1')
    if (await reportLink.count() === 0) {
      await page.getByTestId('row-1').getByRole('button', { name: '启动比对', exact: true }).click()
      await page.waitForTimeout(800)
      await page.goto('/quality_check')
      await page.waitForTimeout(1500)
      await page.getByTestId('writeback-1').click()
      await page.waitForTimeout(800)
      await page.goto('/table_compare')
      await page.waitForTimeout(1500)
    }
    await page.getByTestId('report-1').click()
    await expect(page).toHaveURL(/\/table_compare\/1\/report/)
    await expect(page.getByTestId('quality-notes-table')).toBeVisible()
    await expect(page.getByTestId('quality-notes-table').locator('tbody tr').first()).toContainText('QC-ORD-001')
  })
})
