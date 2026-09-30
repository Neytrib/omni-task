import { PointerActivationConstraints, PointerSensor } from '@dnd-kit/dom';

// Pointer activation covers the card; the existing handle remains the keyboard activator.
export const taskPointerSensor = PointerSensor.configure({
  activatorElements: source => [source.element],
  preventActivation(event, source) {
    const target = event.target;
    if (target instanceof Element) {
      if (target.closest('[data-no-drag]')) return true;
      if (target.closest('[data-task-open]')) return false;
    }
    return PointerSensor.defaults.preventActivation?.(event, source) ?? false;
  },
  activationConstraints(event) {
    return event.pointerType === 'touch'
      ? [new PointerActivationConstraints.Delay({ value: 250, tolerance: 5 })]
      : [new PointerActivationConstraints.Distance({ value: 8 })];
  },
});
