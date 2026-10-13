import { add } from '@/lib';
import { format } from '../format';

export class Button {
  render(): string {
    return format(add(1, 2));
  }
}
