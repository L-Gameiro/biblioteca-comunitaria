import { generateBookCode } from "./bookCode";

describe("generateBookCode", () => {
  test("exemplo da especificação: João Mellão Neto, 1º livro", () => {
    expect(generateBookCode("João Mellão Neto", 0)).toBe("NETJ-001");
  });

  test("segundo livro do mesmo autor incrementa a sequência", () => {
    expect(generateBookCode("João Mellão Neto", 1)).toBe("NETJ-002");
    expect(generateBookCode("João Mellão Neto", 9)).toBe("NETJ-010");
  });

  test("sobrenome comum, sem sufixo geracional", () => {
    expect(generateBookCode("Clarice Lispector", 2)).toBe("LISC-003");
  });

  test("nome com partícula (de, da, dos) usa o último token", () => {
    expect(generateBookCode("Machado de Assis", 0)).toBe("ASSM-001");
  });

  test("autor com um único nome usa o mesmo token duas vezes", () => {
    expect(generateBookCode("Homero", 0)).toBe("HOMH-001");
  });

  test("remove acentos do sobrenome e do primeiro nome", () => {
    // "Eça de Queirós" -> último token "Queirós" -> QUE; primeiro nome "Eça" -> E
    expect(generateBookCode("Eça de Queirós", 0)).toBe("QUEE-001");
  });

  test("sobrenome curto (<3 letras) é completado com X", () => {
    expect(generateBookCode("Ana Li", 0)).toBe("LIXA-001");
  });

  test("sobrenome composto com hífen é tratado como um token só", () => {
    expect(generateBookCode("Ana Paula Souza-Lima", 0)).toBe("SOUA-001");
  });

  test("sequência ultrapassa 999 sem truncar (cresce para 4 dígitos)", () => {
    expect(generateBookCode("João Mellão Neto", 999)).toBe("NETJ-1000");
  });

  test("treatSuffixAsSurname=false usa o sobrenome anterior ao sufixo geracional", () => {
    expect(
      generateBookCode("João Mellão Neto", 0, { treatSuffixAsSurname: false })
    ).toBe("MELJ-001");
    expect(
      generateBookCode("Carlos Andrade Filho", 0, {
        treatSuffixAsSurname: false,
      })
    ).toBe("ANDC-001");
  });

  test("treatSuffixAsSurname=false não afeta autores sem sufixo geracional", () => {
    expect(
      generateBookCode("Clarice Lispector", 0, {
        treatSuffixAsSurname: false,
      })
    ).toBe("LISC-001");
  });

  test("lança erro para nome vazio", () => {
    expect(() => generateBookCode("", 0)).toThrow();
    expect(() => generateBookCode("   ", 0)).toThrow();
  });

  test("lança erro para contagem negativa ou não inteira", () => {
    expect(() => generateBookCode("Autor Teste", -1)).toThrow();
    expect(() => generateBookCode("Autor Teste", 1.5)).toThrow();
  });
});
