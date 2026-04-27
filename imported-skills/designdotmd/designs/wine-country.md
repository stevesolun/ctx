---
version: alpha
name: Wine Country
description: Bordeaux, cream, long golden afternoons.
colors:
  primary: "#3B1520"
  secondary: "#8B5E6A"
  tertiary: "#B8935F"
  neutral: "#F4EBDC"
  surface: "#FAF3E4"
  on-primary: "#FAF3E4"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.75rem
    fontWeight: 500
  body:
    fontFamily: Lora
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    letterSpacing: "0.14em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

An epicurean palette. Deep wine primary, cream paper, gold accent. For luxury goods and long-form storytelling.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#3B1520`):** Headlines and core text.
- **Secondary (`#8B5E6A`):** Borders, captions, and metadata.
- **Tertiary (`#B8935F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4EBDC`):** The page foundation.

## Typography

- **display:** Playfair Display 5rem
- **h1:** Playfair Display 2.75rem
- **body:** Lora 1.05rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
