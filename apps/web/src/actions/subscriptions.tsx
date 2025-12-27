"use server";

import { cacheLife } from 'next/cache'
import { connection } from 'next/server'
import polarService from "@/server/polar";

async function getCustomerStateWithCache(userId: string) {
  'use cache'
  cacheLife('minutes')
  try {
    const customerState = await polarService.getCustomerStateExternal(userId);
    return customerState;
  } catch {
    return null;
  }
}

export async function getCustomerState(userId: string) {
  try {
    // Skip during prerendering
    await connection();
  } catch {
    return null;
  }
  return await getCustomerStateWithCache(userId);
}
